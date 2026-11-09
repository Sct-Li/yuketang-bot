// AES-256-GCM 解 Chromium v10 cookie（node 内置 crypto，无依赖）
// 用法：echo {"key":"<base64 32字节>","blob":"<base64 12字节nonce+密文+16字节tag>"} | node aesgcm.js
// 输出：明文（UTF-8）到 stdout；失败退出码非 0
const crypto = require('crypto');

const chunks = [];
process.stdin.on('data', (d) => chunks.push(d));
process.stdin.on('end', () => {
  try {
    const { key, blob } = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    const k = Buffer.from(key, 'base64');
    const b = Buffer.from(blob, 'base64');
    if (k.length !== 32) throw new Error('key length ' + k.length);
    const nonce = b.subarray(0, 12);
    const tag = b.subarray(b.length - 16);
    const ct = b.subarray(12, b.length - 16);
    const d = crypto.createDecipheriv('aes-256-gcm', k, nonce);
    d.setAuthTag(tag);
    const out = Buffer.concat([d.update(ct), d.final()]);
    process.stdout.write(out.toString('utf8'));
  } catch (e) {
    process.stderr.write('AESGCM_FAIL: ' + e.message);
    process.exit(1);
  }
});
