import { LoaderCircle } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import Cropper, { type Area } from 'react-easy-crop';
import 'react-easy-crop/react-easy-crop.css';
import { exportAvatarCrop, prepareAvatarCrop, releaseAvatarCrop, type AvatarCropSource } from '../lib/avatar-crop';

export function AvatarCropDialog({ file, onSave, onClose }: {
  file: File; onSave: (file: File) => Promise<void>; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [source, setSource] = useState<AvatarCropSource>();
  const [crop, setCrop] = useState({ x: 0, y: 0 });
  const [zoom, setZoom] = useState(1);
  const [area, setArea] = useState<Area>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  useEffect(() => {
    dialog.current?.showModal();
    let disposed = false;
    let prepared: AvatarCropSource | undefined;
    void prepareAvatarCrop(file).then(value => {
      if (disposed) { releaseAvatarCrop(value); return; }
      prepared = value;
      setSource(value);
    }).catch(() => { if (!disposed) setError('无法读取这张图片，请重新选择。'); });
    return () => { disposed = true; if (prepared) releaseAvatarCrop(prepared); };
  }, [file]);
  const save = async () => {
    if (!source || !area || busy) return;
    setBusy(true); setError(undefined);
    try { await onSave(await exportAvatarCrop(source, area)); onClose(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : '头像保存失败，请重试。'); }
    finally { setBusy(false); }
  };
  return <dialog ref={dialog} className="model-config-dialog avatar-crop-dialog" aria-labelledby="avatar-crop-title"
    onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <header><h2 id="avatar-crop-title">裁剪头像</h2></header>
    <div className="avatar-crop-stage" aria-busy={busy || (!source && !error)} inert={busy}>
      {source ? <Cropper image={source.url} crop={crop} zoom={zoom} aspect={1} minZoom={1} maxZoom={3}
        onCropChange={setCrop} onZoomChange={setZoom} onCropAreaChange={(_, pixels) => setArea(pixels)}
        restrictPosition showGrid={false} disableAutomaticStylesInjection
        cropperProps={{ 'aria-label': '拖动图片调整裁剪位置', 'aria-disabled': busy }}
        mediaProps={{ alt: '头像裁剪预览', onError: () => { setArea(undefined); setError('图片预览无法显示，请重新选择。'); } }} />
        : !error && <LoaderCircle size={20} className="avatar-spinner" aria-label="正在读取图片" />}
    </div>
    <div className="avatar-crop-controls"><label htmlFor="avatar-crop-zoom">缩放</label><input id="avatar-crop-zoom" type="range" min={1} max={3} step={0.01}
      value={zoom} disabled={!source || busy} onChange={event => setZoom(Number(event.target.value))} /></div>
    {error && <p className="avatar-crop-error" role="alert">{error}</p>}
    <footer className="form-actions"><button type="button" disabled={busy} onClick={onClose}>取消</button>
      <button type="button" className="primary-action" disabled={!source || !area || busy} onClick={() => void save()}>
        {busy && <LoaderCircle size={12} className="avatar-spinner" />}保存</button></footer>
  </dialog>;
}
