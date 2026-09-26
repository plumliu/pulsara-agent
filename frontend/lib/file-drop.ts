/** Inspect entry metadata during drop, before a directory can become an upload File. */
export function rejectDirectoryDrop(
  transfer: DataTransfer,
  notify: (title: string, message: string) => void,
): boolean {
  const containsDirectory = Array.from(transfer.items ?? []).some(item =>
    item.kind === 'file' && item.webkitGetAsEntry?.()?.isDirectory === true);
  if (!containsDirectory) return false;
  notify('不支持拖入文件夹', '本次拖入未添加。请使用 @ 选择目录，或粘贴完整路径。');
  return true;
}
