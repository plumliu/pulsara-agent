/** The protected port is captured when a partition is allocated, never read from a
 * mutable runtime-origin getter while retiring workers are still alive. */
export function networkPort(url: URL): string {
  return url.port || (url.protocol === 'https:' || url.protocol === 'wss:' ? '443' : '80');
}
export function browserURL(value: unknown, protectedPort: string): string {
  if (typeof value !== 'string') throw new Error('请输入 HTTP(S) 地址。');
  let url: URL;
  try { url = new URL(value.trim()); } catch { throw new Error('请输入带 http:// 或 https:// 的完整地址。'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('仅支持不含用户名和密码的 HTTP(S) 地址。');
  if (networkPort(url) === protectedPort) throw new Error('网页不能访问 Pulsara 工作台控制服务端口。');
  return url.href;
}
export function allowBrowserRequest(value: string, resourceType: string, protectedPort: string, retired: boolean): boolean {
  if (retired) return false;
  try {
    const url = new URL(value);
    if (resourceType === 'mainFrame') { browserURL(value, protectedPort); return true; }
    if (['http:', 'https:', 'ws:', 'wss:'].includes(url.protocol)) return !url.username && !url.password && networkPort(url) !== protectedPort;
    return ['data:', 'blob:'].includes(url.protocol) || (resourceType !== 'mainFrame' && value === 'about:blank');
  } catch { return false; }
}
