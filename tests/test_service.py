"""Offline API contract tests. No real Apple accounts or network requests."""
import hashlib,http.client,io,json,os,tempfile,threading,time,unittest,zipfile
from pathlib import Path
from urllib.parse import urlsplit
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import service

class FakeBackend:
    def __init__(self): self.calls={}
    def authenticate(self,job,code):
        count=self.calls.get(job.id,0);self.calls[job.id]=count+1
        if not code: return {'type':'error','code':'AUTH_OR_2FA'}
        if code!='123456' or count<1: return {'type':'error','code':'FAILED'}
        return {'type':'authenticated','user':{'mock':'private'},'downloadInfo':{'URL':'https://iosapps.itunes.apple.com/synthetic.ipa','metadata':{'bundleDisplayName':'Sample App','bundleShortVersionString':'1.2.3'}}}
    def official(self,song): return song['URL'],1024
    def download(self,job,user):
        target=job.work/'synthetic.ipa'
        with zipfile.ZipFile(target,'w') as z:
            z.writestr('iTunesMetadata.plist','<plist/>');z.writestr('Payload/Fixture.app/SC_Info/Fixture.sinf','synthetic-test-only')
        return target

class Client:
    def __init__(self,port): self.port=port;self.cookie='';self.config={}
    def call(self,method,path,body=None,headers=None):
        data=json.dumps(body).encode() if body is not None else None
        h={'Origin':'https://ios-history.test','Cookie':self.cookie,'X-CSRF-Token':self.config.get('csrfToken',''),'Content-Type':'application/json'}
        h.update(headers or {})
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        conn.request(method,path,body=data,headers=h);response=conn.getresponse();raw=response.read();status=response.status;rh=dict(response.getheaders());conn.close()
        if 'Set-Cookie' in rh:self.cookie=rh['Set-Cookie'].split(';')[0]
        return status,json.loads(raw) if rh.get('Content-Type','').startswith('application/json') and raw else raw,rh
    def session(self):
        status,data,h=self.call('GET','/api/v1/session');assert status==200
        self.config=data;return h
    def envelope(self,data):
        public=serialization.load_pem_public_key(self.config['publicKey'].encode());key=os.urandom(32);nonce=os.urandom(12)
        data=dict(data,createdAt=int(time.time()*1000))
        ciphertext=AESGCM(key).encrypt(nonce,json.dumps(data).encode(),self.config['csrfToken'].encode())
        wrapped=public.encrypt(key,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))
        return {'wrappedKey':service.b64(wrapped),'nonce':service.b64(nonce),'ciphertext':service.b64(ciphertext)}
    def create(self,mode='package'):
        return self.call('POST','/api/v1/jobs',{'appId':'414478124','versionId':'890654806','mode':mode,'consent':True,'envelope':self.envelope({'account':'tester@example.invalid','password':'synthetic-private-password'})})

class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=service.Service(self.tmp.name,'https://ios-history.test',FakeBackend(),min_free=0)
        self.server=service.Server(('127.0.0.1',0),service.handler_for(self.s));self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.a=Client(self.server.server_address[1]);self.b=Client(self.server.server_address[1]);self.a.session();self.b.session()
    def tearDown(self):
        for job in list(self.s.jobs.values()):self.s.cancel(job)
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def wait(self,id,state):
        for _ in range(100):
            status,data,_=self.a.call('GET','/api/v1/jobs/'+id)
            if data.get('state')==state:return data
            time.sleep(.01)
        self.fail('job did not reach '+state)
    def ready(self):
        status,data,_=self.a.create();self.assertEqual(status,202);id=data['id'];self.wait(id,'awaiting_code')
        status,_,_=self.a.call('POST',f'/api/v1/jobs/{id}/code',{'envelope':self.a.envelope({'code':'123456'})});self.assertEqual(status,202)
        self.wait(id,'ready');return id
    def test_encrypted_login_2fa_link_range_and_owner_isolation(self):
        id=self.ready()
        self.assertEqual(self.b.call('GET','/api/v1/jobs/'+id)[0],404)
        status,link,_=self.a.call('POST',f'/api/v1/jobs/{id}/link',{});self.assertEqual(status,200)
        parsed=urlsplit(link['url']);path=parsed.path+'?'+parsed.query
        status,data,h=self.a.call('GET',path);self.assertEqual(status,200);self.assertTrue(data.startswith(b'PK'));self.assertIn('attachment',h['Content-Disposition'])
        self.assertEqual(self.b.call('GET',path)[0],404)
        status,part,h=self.a.call('GET',path,headers={'Range':'bytes=0-3'});self.assertEqual(status,206);self.assertEqual(part,data[:4])
        self.assertEqual(self.a.call('GET',path,headers={'Range':'bytes=999999-9999999'})[0],416)
        self.assertEqual(self.a.call('GET',path+'x')[0],403)
        for file in Path(self.tmp.name).rglob('*'):
            if file.is_file():self.assertNotIn(b'synthetic-private-password',file.read_bytes())
        self.assertEqual(self.s.jobs[id].password,'')
    def test_csrf_origin_encryption_and_concurrency(self):
        self.assertEqual(self.a.call('POST','/api/v1/jobs',{},headers={'X-CSRF-Token':'wrong'})[0],403)
        self.assertEqual(self.a.call('GET','/api/v1/session',headers={'Origin':'https://evil.test'})[0],403)
        self.assertEqual(self.a.call('POST','/api/v1/jobs',{'appId':'414478124','versionId':'890654806','consent':True,'password':'plaintext'})[0],400)
        self.assertEqual(self.a.call('POST','/api/v1/jobs',{'appId':'414478124','versionId':'890654806','consent':True,'envelope':self.b.envelope({'account':'x','password':'x'})})[0],400)
        _,data,_=self.a.create();id=data['id'];self.wait(id,'awaiting_code');self.assertEqual(self.b.create()[0],409)
        self.assertEqual(self.b.call('DELETE','/api/v1/jobs/'+id)[0],404)
        self.assertEqual(self.a.call('DELETE','/api/v1/jobs/'+id)[0],200);self.assertEqual(self.s.jobs[id].password,'')
    def test_expiry_and_completed_file_survives_restart(self):
        id=self.ready();restarted=service.Service(self.tmp.name,'https://ios-history.test',FakeBackend(),min_free=0)
        self.assertEqual(restarted.jobs[id].state,'ready');self.assertEqual(restarted.secret,self.s.secret)
        self.s.jobs[id].expires=time.time()-1
        self.assertEqual(self.a.call('GET','/api/v1/jobs/'+id)[0],410)
    def test_secure_cookie_and_no_credentials_without_session(self):
        h=self.a.session();cookie=h['Set-Cookie']
        for flag in ['Secure','HttpOnly','SameSite=Strict','Path=/']:self.assertIn(flag,cookie)
        no_cookie=Client(self.server.server_address[1]);self.assertEqual(no_cookie.call('GET','/api/v1/jobs')[0],401)
        self.assertEqual(self.a.call('POST','/api/v1/jobs',{'x':'z'*17000})[0],413)
        self.assertEqual(self.a.call('GET','/api/v1/download/../../service.key')[0],404)

    def test_localhost_http_and_public_https_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            local=service.Service(directory,'http://localhost:8088',FakeBackend(),min_free=0)
            _,_,cookie=local.session('',create=True)
            self.assertIn('HttpOnly',cookie);self.assertNotIn('Secure;',cookie);self.assertNotIn('__Host-',cookie)
            with self.assertRaises(ValueError):service.Service(directory,'http://192.168.1.10:8088',FakeBackend(),min_free=0)
    def test_static_page_has_no_php_or_wordpress_dependency(self):
        status,data,headers=self.a.call('GET','/',headers={'Host':'ios-history.test'})
        self.assertEqual(status,200);self.assertIn(b'/assets/catalog.js',data);self.assertNotIn(b'<?php',data);self.assertNotIn(b'wp-json',data)
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])

    def test_official_link_does_not_download_or_persist_ipa(self):
        def forbidden(*args): self.fail('official mode must not download the IPA')
        self.s.backend.download=forbidden
        _,job,_=self.a.create('official');id=job['id'];self.wait(id,'awaiting_code')
        self.a.call('POST',f'/api/v1/jobs/{id}/code',{'envelope':self.a.envelope({'code':'123456'})})
        data=self.wait(id,'ready');self.assertEqual(data['mode'],'official');self.assertEqual(data['fileName'],'Sample App_1.2.3.ipa')
        status,link,_=self.a.call('POST',f'/api/v1/jobs/{id}/link',{})
        self.assertEqual(status,200);self.assertEqual(link['url'],'https://iosapps.itunes.apple.com/synthetic.ipa');self.assertTrue(link['appleExpiryUnknown'])
        self.assertEqual(self.b.call('POST',f'/api/v1/jobs/{id}/link',{})[0],404)
        self.assertFalse(list((self.s.root/id).rglob('*.ipa')));self.assertFalse((self.s.root/id/'result.json').exists())
        self.assertEqual(self.a.call('GET',f'/api/v1/download/{id}?expires=9999999999&signature=x')[0],404)

    def test_filenames_are_safe_and_use_real_bundle_version(self):
        import plistlib
        path=Path(self.tmp.name)/'naming.ipa'
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('iTunesMetadata.plist',plistlib.dumps({'bundleDisplayName':'示例/应用','bundleShortVersionString':'999.0'}))
            z.writestr('Payload/Sample.app/Info.plist',plistlib.dumps({'CFBundleShortVersionString':'1.2.3'}))
        self.assertEqual(service.cli.package_filename(path,'12345','99999'),'示例_应用_1.2.3.ipa')
        name=service.cli.friendly_filename('../bad\r\nname','../../2.0')
        self.assertNotIn('/',name);self.assertNotIn('\n',name);self.assertNotIn('\r',name)
        id=self.ready();self.s.jobs[id].file_name='示例_应用_1.2.3.ipa'
        _,link,_=self.a.call('POST',f'/api/v1/jobs/{id}/link',{})
        self.assertEqual(link['fileName'],'示例_应用_1.2.3.ipa')
        parsed=urlsplit(link['url']);status,_,headers=self.a.call('GET',parsed.path+'?'+parsed.query)
        self.assertEqual(status,200);self.assertIn("filename*=UTF-8''%E7%A4%BA%E4%BE%8B",headers['Content-Disposition'])

if __name__=='__main__':unittest.main()
