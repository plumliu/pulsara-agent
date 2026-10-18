import { desktopHost } from './desktop-host';
const directories = new WeakSet<File>();
export function isNativeDirectory(file: File): boolean { return directories.has(file); }
/** Capture entry metadata synchronously, before directory File metadata is lost. */
export function rejectDirectoryDrop(transfer: DataTransfer, notify: (title: string, message: string) => void): boolean {
  const items = Array.from(transfer.items ?? []);
  const directoryItems = items.filter(item => item.kind === 'file' && item.webkitGetAsEntry?.()?.isDirectory === true);
  if (!directoryItems.length) return false;
  const host = desktopHost();
  if (host) {
    for (const item of directoryItems) {
      const file = item.getAsFile();
      if (file && host.pathForFile(file)) {
        directories.add(file);
        for (const transferred of Array.from(transfer.files)) if (host.pathForFile(transferred) === host.pathForFile(file)) directories.add(transferred);
      } else { notify('目录未加入草稿', '无法取得该目录的原始路径，请使用目录选择器。'); return true; }
    }
    return false;
  }
  notify('不支持拖入文件夹', '本次拖入未添加。请使用 @ 选择目录，或粘贴完整路径。');
  return true;
}
