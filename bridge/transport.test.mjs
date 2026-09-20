import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {spawn} from 'node:child_process';
import {appleURL,configQuote} from './transport.mjs';

test('authentication destinations reject lookalikes, credentials, HTTP and foreign ports', () => {
    assert.equal(appleURL('https://auth.itunes.apple.com/auth/v1/native/fast/'),'https://auth.itunes.apple.com/auth/v1/native/fast/');
    for (const value of ['https://itunes.apple.com.evil.test/', 'http://auth.itunes.apple.com/', 'https://user@auth.itunes.apple.com/', 'https://auth.itunes.apple.com:8443/', 'https://127.0.0.1/']) {
        assert.throws(()=>appleURL(value));
    }
});

test('curl stdin config preserves exact signed UTF-8 body bytes and headers', async () => {
    const body = '<plist>\n<string>测试 \\ " &: fake-password</string>\r\n</plist>';
    let received;
    const server = http.createServer((req,res)=>{
        const chunks=[];
        req.on('data',chunk=>chunks.push(chunk));
        req.on('end',()=>{received={body:Buffer.concat(chunks).toString('utf8'),header:req.headers['x-test']};res.end('ok');});
    });
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    try {
        const child = spawn('/usr/bin/curl',['--disable','--config','-'],{stdio:['pipe','pipe','pipe']});
        let stdout=''; child.stdout.on('data',data=>stdout+=data);
        const done = new Promise((resolve,reject)=>{child.on('error',reject);child.on('close',resolve);});
        child.stdin.end(`silent\nmax-time = 5\nnoproxy = "*"\nurl = "http://127.0.0.1:${server.address().port}/"\nheader = ${configQuote('X-Test: fake-secret')}\ndata-binary = ${configQuote(body)}\n`);
        assert.equal(await done,0); assert.equal(stdout,'ok');
        assert.deepEqual(received,{body,header:'fake-secret'});
    } finally { await new Promise(resolve=>server.close(resolve)); }
});
