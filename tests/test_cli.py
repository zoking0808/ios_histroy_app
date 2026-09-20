"""Offline integration checks; uses a synthetic IPA and never contacts Apple."""
import argparse
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ios_download', ROOT / 'ios_download.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'; self.state.mkdir()
        self.engine = self.state / 'engine'; (self.engine / 'src').mkdir(parents=True)
        (self.engine / 'package.json').write_text('{"type":"module"}')
        (self.engine / 'src/gsa.js').write_text('export function cleanup() {}')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as archive:
            archive.writestr('iTunesMetadata.plist', '<plist/>')
            archive.writestr('Payload/Fixture.app/Info.plist', '<plist/>')
            archive.writestr('Payload/Fixture.app/SC_Info/Fixture.sinf', 'synthetic-test-only')
        data = base64.b64encode(buf.getvalue()).decode()
        code = '''import fs from 'node:fs/promises';
export class Ipa {
 constructor(creds) { this.creds = creds; }
 async login() { if(this.creds.CODE !== '123456') { const e = new Error('Mock 2FA challenge'); e.code='NEEDS_2FA'; throw e; } }
 applyUser(user,cached) {this.user=user;this.usedCachedSession=cached;}
 async saveSession() {}
 async info(id, version) { return { URL:'https://iosapps.itunes.apple.com/test-only', metadata:{itemId:id,softwareVersionExternalIdentifier:version},sinfs:[{sinf:'fixture'}] }; }
 async run({APPID,appVerId}) { await this.info(APPID,appVerId); await fs.writeFile(this.out,Buffer.from(DATA,'base64')); }
}'''.replace('DATA', json.dumps(data))
        (self.engine / 'src/ipa.js').write_text(code)
        (self.engine / 'src/device.js').write_text('export function getDeviceGuid(){return "AABBCCDDEEFF"}')
        self.node = cli.runtime_node()
        self.args = argparse.Namespace(app_id='414478124',version_id='890654806',output=str(self.root / 'downloads'))
        self.patches = [patch.object(cli,'STATE',self.state),patch.object(cli,'ENGINE',self.engine)]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def run_interaction(self, inputs, secrets):
        output = io.StringIO()
        with patch('builtins.input',side_effect=inputs),patch.object(cli.getpass,'getpass',side_effect=secrets),contextlib.redirect_stdout(output):
            result = cli.download_interactive(self.node,self.args)
        self.assertEqual(list((self.state / 'jobs').iterdir()), [])
        self.assertNotIn('fake-password-97', output.getvalue())
        self.assertNotIn('tester@example.invalid', output.getvalue())
        return result,output.getvalue()

    def test_two_factor_retry_download_and_cleanup(self):
        path, text = self.run_interaction(['tester@example.invalid','1'],['fake-password-97','123456'])
        self.assertTrue(path.is_file())
        self.assertTrue(zipfile.is_zipfile(path))
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertIn('双重认证',text)
        second,_ = self.run_interaction(['tester@example.invalid','1'],['fake-password-97','123456'])
        self.assertNotEqual(path,second)
        self.assertTrue(path.is_file())

    def test_cancel_at_otp_leaves_no_ipa_or_session(self):
        path,_ = self.run_interaction(['tester@example.invalid','q'],['fake-password-97'])
        self.assertIsNone(path)
        self.assertFalse((self.root / 'downloads').exists())

    def test_wrong_returned_version_never_becomes_success(self):
        source = self.engine / 'src/ipa.js'
        source.write_text(source.read_text().replace('softwareVersionExternalIdentifier:version','softwareVersionExternalIdentifier:"1"'))
        path,text = self.run_interaction(['tester@example.invalid','1'],['fake-password-97','123456'])
        self.assertIsNone(path)
        self.assertIn('与目标不一致',text)
        self.assertFalse((self.root / 'downloads').exists())

    def test_input_and_environment_boundaries(self):
        for value in ['8.0.78','123/../../x','0','99999999999999999999999']:
            with self.assertRaises(ValueError): cli.validated_id(value,version=True)
        with patch.dict(os.environ,{'IPA_ALLOW_APP_ACQUIRE':'1','IPA_APP_IS_FREE':'1','NODE_TLS_REJECT_UNAUTHORIZED':'0','APPLE_PASSWORD':'fake'}):
            env = cli.base_environment(self.node)
        for key in ['IPA_ALLOW_APP_ACQUIRE','IPA_APP_IS_FREE','NODE_TLS_REJECT_UNAUTHORIZED','APPLE_PASSWORD']:
            self.assertNotIn(key,env)

    def test_new_auth_bridge_only_uses_private_pipe_and_can_retry(self):
        helper=self.state/'ipatool-auth'
        helper.write_text('''#!/usr/bin/env python3
import json,sys
challenged=False
for line in sys.stdin:
 data=json.loads(line)
 if data['code']!='123456':
  challenged=True
  print(json.dumps({'ok':False,'code':'AUTH_OR_2FA','message':'auth code is required'}),flush=True)
 else:
  if not challenged:
   print(json.dumps({'ok':False,'code':'SESSION_LOST','message':'challenge session was lost'}),flush=True)
  else:
   print(json.dumps({'ok':True,'user':{'authHeaders':{'X-Token':'fake-private-token','X-Dsid':'fake-dsid'}}}),flush=True)
  break
''')
        helper.chmod(0o700)
        self.args.auth_engine='ipatool'
        path,text=self.run_interaction(['tester@example.invalid','1'],['fake-password-97','123456'])
        self.assertTrue(path.is_file())
        self.assertNotIn('fake-private-token',text)

    def test_login_only_does_not_ask_for_app_or_write_download(self):
        self.args.login_only=True
        result,text=self.run_interaction(['tester@example.invalid','1'],['fake-password-97','123456'])
        self.assertIs(result,True)
        self.assertFalse((self.root/'downloads').exists())

    def test_no_otp_prompt_when_apple_accepts_password(self):
        self.args.login_only=True
        source=self.engine/'src/ipa.js'
        source.write_text(source.read_text().replace("if(this.creds.CODE !== '123456')", 'if(false)'))
        result,text=self.run_interaction(['tester@example.invalid'],['fake-password-97'])
        self.assertIs(result,True)
        self.assertNotIn('双重认证',text)

    def test_password_is_sent_before_any_otp_prompt(self):
        self.args.login_only=True
        real=cli.run_worker
        sent=[]
        def record(node,payload,job):
            sent.append(payload['code'])
            return real(node,payload,job)
        def ask(prompt):
            if '验证码' in prompt:
                self.assertEqual(sent,[''])
                return '123456'
            return 'fake-password-97'
        with patch('builtins.input',side_effect=['tester@example.invalid','1']),patch.object(cli.getpass,'getpass',side_effect=ask),patch.object(cli,'run_worker',side_effect=record),contextlib.redirect_stdout(io.StringIO()):
            self.assertIs(cli.download_interactive(self.node,self.args),True)
        self.assertEqual(sent,['','123456'])


if __name__ == '__main__': unittest.main()
