export interface WorkspaceDirectoryCandidate {
  path: string;
  linuxPath: string | null;
}

function distributionName(value: string): string {
  if (!value || /[\\/\x00-\x1f\x7f]/.test(value)) throw new Error('无效的 WSL 发行版名称。');
  return value;
}

function windowsComponent(value: string): void {
  const base = value.split('.')[0].trimEnd();
  if (/[<>:"|?*]/.test(value) || /[. ]$/.test(value)
      || /^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$/i.test(base)) throw new Error('此 Linux 文件名无法无歧义地表示为 Windows 路径；请使用文件预览。');
}

/** Lexical domain conversion only. The Linux owner still verifies the target. */
export function toBackendPath(input: unknown, distribution: string): string {
  distributionName(distribution);
  if (typeof input !== 'string' || !input || /[\x00-\x1f\x7f]/.test(input)) throw new Error('请输入有效的 Linux 或同发行版 UNC 路径。');
  let components: string[];
  if (input.startsWith('/') && !input.startsWith('//') && !input.includes('\\')) {
    components = input.slice(1).split('/');
  } else {
    const match = /^\\\\(wsl\.localhost|wsl\$)\\([^\\]+)\\(.*)$/i.exec(input);
    if (!match || match[2].toLowerCase() !== distribution.toLowerCase()) throw new Error('仅支持已选 WSL 发行版中的 Linux 路径。');
    components = match[3].split('\\');
    if (components.some(part => part.includes('/'))) throw new Error('路径分隔符不明确。');
    for (const part of components) windowsComponent(part);
  }
  if (components.length === 1 && components[0] === '') return '/';
  if (components.some(part => !part || part === '.' || part === '..')) throw new Error('路径不能包含空组件或点组件。');
  return '/' + components.join('/');
}

export function toWindowsPath(input: unknown, distribution: string): string {
  const linux = toBackendPath(input, distribution);
  if (typeof input !== 'string' || !input.startsWith('/') || input.startsWith('//')) throw new Error('需要 Linux 绝对路径。');
  for (const part of linux.slice(1).split('/')) windowsComponent(part);
  return '\\\\wsl.localhost\\' + distributionName(distribution) + linux.replaceAll('/', '\\');
}
