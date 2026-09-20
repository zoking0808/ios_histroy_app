// This narrow export keeps download metadata resolution in the same authenticated
// ipatool client, including its validated redownload/updateProduct recovery.
package appstore

import (
    "errors"
    "fmt"
    "net/url"
    "strings"
)

type Study97Sinf struct {
    ID int64 `json:"id"`
    Data []byte `json:"sinf"`
}
type Study97Song struct {
    URL string `json:"URL"`
    MD5 string `json:"md5"`
    Metadata map[string]interface{} `json:"metadata"`
    Sinfs []Study97Sinf `json:"sinfs"`
}
func Study97Resolve(store AppStore, account Account, appID int64, versionID string) (*Study97Song,error) {
    client,ok:=store.(*appstore);if !ok{return nil,errors.New("unsupported store client")}
    mac,err:=client.machine.MacAddress();if err!=nil{return nil,err}
    guid,_,err:=machineIdentity(mac);if err!=nil{return nil,err}
    result,_,err:=client.sendDownloadProduct(account,App{ID:appID},guid,versionID,PlatformIPhone)
    if err!=nil{return nil,err}
    data:=result.Data
    if data.FailureType==FailureTypeLicenseNotFound{return nil,ErrLicenseRequired}
    if data.FailureType==FailureTypePasswordTokenExpired || data.FailureType==FailureTypeSignInRequired || data.FailureType==FailureTypeDeviceVerificationFailed {return nil,ErrPasswordTokenExpired}
    if data.FailureType!="" {return nil,fmt.Errorf("Apple download failure %s",data.FailureType)}
    if len(data.Items)!=1{return nil,errors.New("Apple did not return exactly one available package for the selected version")}
    item:=data.Items[0]
    if fmt.Sprint(item.Metadata["itemId"])!=fmt.Sprint(appID) || fmt.Sprint(item.Metadata["softwareVersionExternalIdentifier"])!=versionID {
        return nil,errors.New("Apple download metadata does not match requested app/version")
    }
    target,err:=url.Parse(item.URL)
    if err!=nil || target.Scheme!="https" || target.User!=nil || (target.Port()!="" && target.Port()!="443") || !(strings.HasSuffix(target.Hostname(),".apple.com") || strings.HasSuffix(target.Hostname(),".mzstatic.com")) {
        return nil,errors.New("unexpected Apple download URL")
    }
    sinfs:=make([]Study97Sinf,0,len(item.Sinfs))
    for _,s:=range item.Sinfs {sinfs=append(sinfs,Study97Sinf{ID:s.ID,Data:s.Data})}
    return &Study97Song{URL:item.URL,MD5:item.HashMD5,Metadata:item.Metadata,Sinfs:sinfs},nil
}
