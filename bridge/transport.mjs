// Local adapter: keep plist bodies and auth headers out of curl argv/files.
import {execFileSync} from 'node:child_process';
import {mkdtempSync, readFileSync, existsSync, rmSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export function appleURL(value) {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password || (url.port && url.port !== '443') ||
        !(url.hostname === 'itunes.apple.com' || url.hostname.endsWith('.itunes.apple.com'))) {
        throw new Error('拒绝向非 Apple 商店 HTTPS 地址发送认证数据');
    }
    return url.href;
}
export function configQuote(value) {
    const text = String(value);
    if (text.includes('\0')) throw new Error('Invalid NUL in request');
    return '"' + text.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n').replace(/\r/g, '\\r').replace(/\t/g, '\\t') + '"';
}
function systemProxy() {
    if (process.platform !== 'darwin') return process.env.HTTPS_PROXY || process.env.https_proxy || '';
    try {
        const config = execFileSync('/usr/sbin/scutil', ['--proxy'], {timeout:5000, encoding:'utf8'});
        const host = config.match(/HTTPSProxy\s*:\s*(\S+)/)?.[1];
        const port = config.match(/HTTPSPort\s*:\s*(\d+)/)?.[1];
        if (/HTTPSEnable\s*:\s*1/.test(config) && host && port) return `http://${host}:${port}`;
    } catch {}
    return process.env.HTTPS_PROXY || process.env.https_proxy || '';
}
export function curlRequest(method, value, {headers = {}, body = null, follow = false, timeout = 30, jar = null, redirects = 0} = {}) {
    const url = appleURL(value);
    const dir = mkdtempSync(path.join(os.tmpdir(), 'study97-http-'));
    const output = path.join(dir, 'body');
    const head = path.join(dir, 'headers');
    const config = ['silent', 'show-error', 'proto = "=https"',
        `url = ${configQuote(url)}`, `request = ${configQuote(method)}`,
        `max-time = ${timeout}`, 'connect-timeout = 15',
        `output = ${configQuote(output)}`, `dump-header = ${configQuote(head)}`, 'write-out = "%{http_code}"'];
    if (jar) config.push(`cookie = ${configQuote(jar)}`, `cookie-jar = ${configQuote(jar)}`);
    const proxy = systemProxy();
    if (proxy) config.push(`proxy = ${configQuote(proxy)}`);
    for (const [key, value] of Object.entries(headers)) {
        if (/[\r\n]/.test(key + value)) throw new Error('Invalid request header');
        config.push(`header = ${configQuote(`${key}: ${value}`)}`);
    }
    if (body !== null) config.push(`data-binary = ${configQuote(Buffer.isBuffer(body) ? body.toString('utf8') : body)}`);
    let response;
    let curlExit = 0;
    try {
        let status = 0;
        try {
            status = Number(execFileSync('/usr/bin/curl', ['--disable', '--config', '-'], {
                input:config.join('\n') + '\n', encoding:'utf8', timeout:(timeout + 5) * 1000,
                stdio:['pipe','pipe','pipe'], maxBuffer:1024 * 1024,
            }).trim());
        } catch (error) { curlExit = Number.isInteger(error.status) ? error.status : -1; }
        response = {status, headers:existsSync(head) ? readFileSync(head,'utf8') : '', body:existsSync(output) ? readFileSync(output) : Buffer.alloc(0)};
    } finally { rmSync(dir, {recursive:true, force:true}); }
    // Strict allowlist: never include raw body/headers, cookies, URLs with queries or account values.
    const text = response.body.toString('utf8').trim();
    const failure = text.match(/<key>failureType<\/key>\s*<(?:string|integer)>(-?\d+)<\//)?.[1] || '';
    process.emit('study97-http', {
        host:new URL(url).hostname, method, status:response.status, curlExit,
        endpoint:url.includes('bag.xml') ? 'bag' : url.includes('authenticate') || url.includes('/auth/') ? 'authenticate' : 'store',
        bytes:response.body.length, format:/<plist[\s>]/.test(text) ? 'plist' : /^\s*[\[{]/.test(text) ? 'json' : /<html/i.test(text) ? 'html' : text ? 'other' : 'empty',
        failureType:failure, hasToken:/<key>passwordToken<\/key>/.test(text), hasDsid:/<key>dsPersonId<\/key>/.test(text),
        proxy:proxy ? 'configured' : 'none',
    });
    if (follow && [301,302,307,308].includes(response.status)) {
        const location = response.headers.match(/^location:\s*(.+)$/im)?.[1]?.trim();
        if (!location || redirects >= 4) throw new Error('Apple 商店重定向异常');
        return curlRequest(method, new URL(location, url).href, {headers,body,follow,timeout,jar,redirects:redirects+1});
    }
    return response;
}
