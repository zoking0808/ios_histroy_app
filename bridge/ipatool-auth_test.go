package main

import (
    "errors"
    "net/http"
    "net/http/cookiejar"
    "net/url"
    "testing"
    "time"
    "github.com/majd/ipatool/v2/internal/sap"
    "github.com/majd/ipatool/v2/pkg/appstore"
)
type testSigner struct{ signs, closes int }
func (s *testSigner) Sign(b []byte)([]byte,error){if s.closes>0{return nil,errors.New("closed")};s.signs++;return b,nil}
func (s *testSigner) Close()error{s.closes++;return nil}
func TestPasswordAndOTPShareSigner(t *testing.T){
    signer:=&testSigner{};created:=0
    session:=&signerSession{create:func(appstore.SAPConfig,[]byte)(sap.ActionSigner,error){created++;return signer,nil}}
    config:=appstore.SAPConfig{Version:200};hardware:=[]byte{1,2,3,4,5,6}
    first,err:=session.acquire(config,hardware);if err!=nil{t.Fatal(err)}
    _,_=first.Sign([]byte("password"));_=first.Close()
    second,err:=session.acquire(config,hardware);if err!=nil{t.Fatal(err)}
    if _,err=second.Sign([]byte("password+OTP"));err!=nil{t.Fatal(err)}
    _=second.Close()
    if created!=1 || signer.closes!=0 || signer.signs!=2{t.Fatalf("session reset: created=%d closes=%d signs=%d",created,signer.closes,signer.signs)}
    session.close();session.close()
    if signer.closes!=1{t.Fatal("signer must be closed exactly once")}
}
func TestRejectChangedIdentityDuringChallenge(t *testing.T){
    session:=&signerSession{create:func(appstore.SAPConfig,[]byte)(sap.ActionSigner,error){return &testSigner{},nil}}
    _,_=session.acquire(appstore.SAPConfig{Version:200},[]byte{1})
    if _,err:=session.acquire(appstore.SAPConfig{Version:200},[]byte{2});err==nil{t.Fatal("changed device reused session")}
    session.close()
}
func TestChallengeCookiesSurviveAndExpiredCookiesAreRemoved(t *testing.T){
    inner,_:=cookiejar.New(nil);jar:=&memoryJar{inner,make(map[string]string)}
    u,_:=url.Parse("https://buy.itunes.apple.com/WebObjects/MZFinance.woa/wa/authenticate")
    jar.SetCookies(u,[]*http.Cookie{{Name:"challenge",Value:"synthetic",Path:"/",Secure:true}})
    _=jar.Save()
    if len(jar.Cookies(u))!=1 || len(jar.records)!=1{t.Fatal("challenge cookie missing")}
    jar.SetCookies(u,[]*http.Cookie{{Name:"challenge",Value:"",Path:"/",MaxAge:-1,Expires:time.Unix(1,0)}})
    if len(jar.Cookies(u))!=0 || len(jar.records)!=0{t.Fatal("expired cookie retained")}
}
