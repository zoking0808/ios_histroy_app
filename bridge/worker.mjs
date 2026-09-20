// Credentials arrive through an anonymous stdin pipe, never through argv/env.
import {pathToFileURL} from 'node:url';
import {stat} from 'node:fs/promises';
import path from 'node:path';
import {format} from 'node:util';

const write = process.stdout.write.bind(process.stdout);
const emit = (type, data = {}) => write(JSON.stringify({type,...data}) + '\n');
let phase = 'init', acquisition = false;
// Discard upstream account/name/raw-error logging. Forward only generic progress.
console.log = (...args) => {
    const message = format(...args);
    if (message.includes('@@IPA:requires-acquisition')) acquisition = true;
    if (message.includes('@@IPA:phase=packaging')) emit('phase',{message:'正在校验并写入账号授权数据…'});
};
console.error = () => {};
process.stdout.write = (chunk, encoding, callback) => {
    const text = String(chunk);
    if (text.startsWith('下载进度:')) emit('progress',{message:text.trim()});
    const done = typeof encoding === 'function' ? encoding : callback;
    if (done) done();
    return true;
};

let cleanup = () => {};
try {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (Number.isSafeInteger(input.maxFileSize) && input.maxFileSize > 0) process.env.STUDY97_MAX_DOWNLOAD_BYTES = String(input.maxFileSize);
    if (input.diagnose) process.on('study97-http', details => emit('diagnostic',details));
    const engine = path.resolve(process.env.STUDY97_IPA_ENGINE);
    const {Ipa} = await import(pathToFileURL(path.join(engine,'src/ipa.js')));
    const {cleanup: clear} = await import(pathToFileURL(path.join(engine,'src/gsa.js')));
    cleanup = clear;
    if (input.action === 'check') { emit('success',{message:'下载模块加载正常；未发起 Apple 登录。'}); }
    else {
        if (input.action !== 'login' && (!/^[1-9]\d{4,14}$/.test(input.appId) || !/^[1-9]\d{0,19}$/.test(input.versionId))) throw new Error('App ID 或版本 ID 无效');
        if (!input.appleAccount || !input.password || !input.sessionKey) throw new Error('登录参数不完整');
        const app = new Ipa({APPLE_ID:input.appleAccount,PASSWORD:input.password,CODE:input.code || '',SESSION_KEY:input.sessionKey});
        if (input.authEngine === 'ipatool') {
            app.login = async () => {
                const user = input.authorizedUser;
                if (!user?.authHeaders?.['X-Token'] || !user?.authHeaders?.['X-Dsid']) throw new Error('缺少完整认证会话，请从Python入口开始登录');
                app.applyUser(user,false);
                await app.saveSession(user);
            };
        }
        // No automatic acquisition/purchase. A missing license is actionable guidance.
        process.env.IPA_ALLOW_APP_ACQUIRE = '0';
        delete process.env.IPA_APP_IS_FREE;
        phase = 'login'; emit('phase',{message:'正在连接 Apple 并验证账户…'});
        await app.login();
        if (input.action === 'login') {
            emit('success',{login:true,message:'Apple 账户登录成功。'});
        } else {
        phase = 'download'; emit('phase',{message:'登录成功，正在获取指定版本…'});
        const originalInfo = app.info.bind(app);
        app.info = async (appId, versionId) => {
            const song = input.resolvedSong || await originalInfo(appId, versionId);
            const actual = String(song?.metadata?.softwareVersionExternalIdentifier ?? song?.softwareVersionExternalIdentifier ?? '');
            if (actual !== versionId) {
                const error = new Error(actual ? `Apple 返回的版本 ID ${actual} 与目标不一致，已停止下载。` : 'Apple 未返回可核验的版本 ID，已停止下载。');
                error.code = 'VERSION_MISMATCH'; throw error;
            }
            const actualApp = String(song?.metadata?.itemId ?? song?.metadata?.softwareId ?? '');
            if (actualApp && actualApp !== appId) throw new Error('Apple 返回的应用 ID 与目标不一致');
            const url = new URL(song.URL);
            if (url.protocol !== 'https:' || url.username || url.password ||
                !(url.hostname.endsWith('.apple.com') || url.hostname.endsWith('.mzstatic.com'))) {
                throw new Error('Apple 返回的下载地址不符合预期');
            }
            if (!song?.sinfs?.[0]?.sinf) throw new Error('Apple 没有返回该账号的安装包授权数据');
            app.out = path.join(input.staging, `${appId}-${versionId}.ipa`);
            return song;
        };
        await app.run({dir:input.staging,APPID:input.appId,appVerId:input.versionId});
        const info = await stat(app.out);
        if (info.size < 1) throw new Error('下载结果为空');
        emit('success',{path:app.out,size:info.size});
        }
    }
} catch (error) {
    emit('error',{phase,code:acquisition ? 'LICENSE_REQUIRED' : (error.code || 'FAILED'),message:String(error.message || '下载失败').slice(0,1200)});
    process.exitCode = 1;
} finally {
    cleanup();
    await new Promise(resolve => write('',resolve));
    process.exit(process.exitCode || 0);
}
