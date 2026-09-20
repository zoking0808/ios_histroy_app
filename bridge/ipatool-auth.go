// Local stdin bridge to pinned ipatool. No OS keychain or persistent credentials.
package main

import (
    "context"
    "bytes"
    "encoding/hex"
    "encoding/json"
    "errors"
    "fmt"
    "io"
    "net/http"
    "net/http/cookiejar"
    "net/url"
    "os"
    "strconv"
    "strings"
    "time"

    "github.com/majd/ipatool/v2/internal/sap"
    "github.com/majd/ipatool/v2/pkg/appstore"
    "github.com/majd/ipatool/v2/pkg/util/operatingsystem"
)

type input struct { Action, AppleAccount, Password, Code, Guid, AppID, VersionID string; ResolveDownload bool }

// AppStore.Login closes the signer it borrows. Keep the underlying SAP session
// alive across the password and OTP requests; release only when this CLI ends.
type signerLease struct { sap.ActionSigner }
func (signerLease) Close() error { return nil }
type signerSession struct {
    active sap.ActionSigner
    config appstore.SAPConfig
    hardware []byte
    create func(appstore.SAPConfig, []byte) (sap.ActionSigner,error)
}
func (s *signerSession) acquire(config appstore.SAPConfig, hardware []byte) (appstore.ActionSigner,error) {
    if s.active != nil {
        if s.config != config || !bytes.Equal(s.hardware,hardware) {return nil,errors.New("Apple签名配置变化，请重新开始登录")}
        return signerLease{s.active},nil
    }
    signer,err:=s.create(config,hardware);if err!=nil{return nil,err}
    s.active=signer;s.config=config;s.hardware=append([]byte(nil),hardware...)
    return signerLease{signer},nil
}
func (s *signerSession) close() {
    if s.active!=nil {_=s.active.Close();s.active=nil}
    for i:=range s.hardware{s.hardware[i]=0}
}
type memoryKeys struct{}
func (memoryKeys) Get(string) ([]byte,error) { return nil,errors.New("no saved account") }
func (memoryKeys) Set(string,[]byte) error { return nil }
func (memoryKeys) Remove(string) error { return nil }
type identity struct { guid string }
func (m identity) MacAddress() (string,error) {
    bytes,err:=hex.DecodeString(m.guid); if err!=nil || len(bytes)!=6 { return "",errors.New("invalid device GUID") }
    parts:=make([]string,6); for i,b:=range bytes {parts[i]=fmt.Sprintf("%02x",b)}
    return strings.Join(parts,":"),nil
}
func (identity) HomeDirectory() string { return "" }
func (identity) ReadPassword(int) ([]byte,error) {return nil,errors.New("stdin credentials only")}
type memoryJar struct { *cookiejar.Jar; records map[string]string }
func (j *memoryJar) Save() error {return nil}
func (j *memoryJar) SetCookies(u *url.URL, cookies []*http.Cookie) {
    j.Jar.SetCookies(u,cookies)
    for _,c:=range cookies {
        domain:=c.Domain; sub:="FALSE"; if domain=="" {domain=u.Hostname()} else {sub="TRUE";domain="."+strings.TrimPrefix(domain,".")}
        path:=c.Path; if path=="" {path="/"}; secure:="FALSE"; if c.Secure {secure="TRUE"}
        key:=domain+path+c.Name
        if c.MaxAge<0 || (!c.Expires.IsZero() && c.Expires.Before(time.Now())) {delete(j.records,key);continue}
        expiry:=int64(0); if !c.Expires.IsZero() {expiry=c.Expires.Unix()}
        if strings.ContainsAny(domain+path+c.Name+c.Value,"\r\n\t") {continue}
        j.records[key]=fmt.Sprintf("%s\t%s\t%s\t%s\t%d\t%s\t%s",domain,sub,path,secure,expiry,c.Name,c.Value)
    }
}
func output(value any) { _=json.NewEncoder(os.Stdout).Encode(value) }
func main() {
    var in input
    decoder:=json.NewDecoder(io.LimitReader(os.Stdin,3*65536))
    if err:=decoder.Decode(&in);err!=nil {output(map[string]any{"ok":false,"code":"INPUT","message":"invalid input"});return}
    if in.Action=="check" {output(map[string]any{"ok":true,"sessionProtocol":3});return}
    jar,_:=cookiejar.New(nil); cookies:=&memoryJar{jar,make(map[string]string)}
    signing:=&signerSession{create:func(config appstore.SAPConfig,hardware []byte)(sap.ActionSigner,error){
        return sap.NewSigner(context.Background(),sap.Config{SetupURL:config.SetupURL,CertificateURL:config.CertificateURL,Version:config.Version,HardwareID:hardware})
    }}
    defer signing.close()
    store:=appstore.NewAppStore(appstore.Args{Keychain:memoryKeys{},CookieJar:cookies,OperatingSystem:operatingsystem.New(),Machine:identity{in.Guid},ActionSignerFactory:signing.acquire})
    if in.Action=="prepare" {
        bag,err:=store.Bag(appstore.BagInput{}); if err!=nil {output(map[string]any{"ok":false,"code":"PREPARE","message":err.Error()});return}
        hardware,_:=hex.DecodeString(in.Guid)
        signer,err:=sap.NewSigner(context.Background(),sap.Config{SetupURL:bag.SAPConfig.SetupURL,CertificateURL:bag.SAPConfig.CertificateURL,Version:bag.SAPConfig.Version,HardwareID:hardware})
        if err==nil {_,err=signer.Sign([]byte("study97-sap-readiness-check"));_ = signer.Close()}
        if err!=nil {output(map[string]any{"ok":false,"code":"PREPARE","message":err.Error()});return}
        output(map[string]any{"ok":true,"prepared":true});return
    }
    initialAccount,initialGuid:=in.AppleAccount,in.Guid
    for attempt:=0;attempt<3;attempt++ {
    if in.AppleAccount=="" || in.Password=="" || in.AppleAccount!=initialAccount || in.Guid!=initialGuid {
        output(map[string]any{"ok":false,"code":"INPUT","message":"会话中的账号或设备参数无效"});return
    }
    cookieCount:=len(cookies.records)
    signerReused:=signing.active!=nil
    result,err:=store.Login(appstore.LoginInput{Email:in.AppleAccount,Password:in.Password,AuthCode:in.Code})
    diagnostics:=map[string]any{"request":attempt+1,"cookiesBefore":cookieCount,"cookiesAfter":len(cookies.records),"signerReused":signerReused}
    if err!=nil {
        code:="APPLE_AUTH_FAILED"; if errors.Is(err,appstore.ErrAuthCodeRequired) {code="AUTH_OR_2FA"}
        output(map[string]any{"ok":false,"code":code,"message":err.Error(),"diagnostics":diagnostics})
        if code!="AUTH_OR_2FA" || attempt==2 {return}
        var next input
        if err:=decoder.Decode(&next);err!=nil {return}
        in=next
        continue
    }
    account:=result.Account
    var song *appstore.Study97Song
    if in.ResolveDownload {
        appID,parseErr:=strconv.ParseInt(in.AppID,10,64)
        if parseErr!=nil || appID<=0 || in.VersionID=="" {output(map[string]any{"ok":false,"code":"INPUT","message":"invalid app/version"});return}
        song,err=appstore.Study97Resolve(store,account,appID,in.VersionID)
        if err!=nil {
            code:="DOWNLOAD_UNAVAILABLE"
            if errors.Is(err,appstore.ErrLicenseRequired) {code="DOWNLOAD_LICENSE_UNCONFIRMED"}
            if errors.Is(err,appstore.ErrPasswordTokenExpired) {code="DOWNLOAD_AUTH_EXPIRED"}
            output(map[string]any{"ok":false,"code":code,"message":err.Error(),"diagnostics":diagnostics});return
        }
    }
    lines:=[]string{"# Netscape HTTP Cookie File"};for _,line:=range cookies.records {lines=append(lines,line)}
    // Only consumed by the Node bridge over a private pipe; never print to the terminal.
    output(map[string]any{"ok":true,"diagnostics":diagnostics,"downloadInfo":song,"user":map[string]any{
        "accountInfo":map[string]any{"appleId":account.Email,"address":map[string]string{"firstName":"","lastName":""}},
        "dsPersonId":account.DirectoryServicesID,"pod":account.Pod,
        "authHeaders":map[string]string{"X-Dsid":account.DirectoryServicesID,"iCloud-DSID":account.DirectoryServicesID,"X-Token":account.PasswordToken,"X-Apple-Store-Front":account.StoreFront},
        "cookieText":strings.Join(lines,"\n")+"\n",
    }})
    return
    }
}
