import type { Area } from 'react-easy-crop';

export interface AvatarCropSource {
  image: ImageBitmap;
  url: string;
}

function canvasBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => canvas.toBlob(
    blob => blob ? resolve(blob) : reject(new Error('图片无法导出，请重试。')), 'image/png',
  ));
}

export async function prepareAvatarCrop(file: File): Promise<AvatarCropSource> {
  // The browser decoder owns EXIF orientation and the first animation frame.
  // Freeze that decoded frame so the crop preview and exported pixels agree.
  const image = await createImageBitmap(file, { imageOrientation: 'from-image' });
  try {
    const canvas = document.createElement('canvas');
    canvas.width = image.width;
    canvas.height = image.height;
    const context = canvas.getContext('2d');
    if (!context) throw new Error('当前浏览器无法处理图片。');
    context.drawImage(image, 0, 0);
    const blob = await canvasBlob(canvas);
    return { image, url: URL.createObjectURL(blob) };
  } catch (error) { image.close(); throw error; }
}

export function releaseAvatarCrop(source: AvatarCropSource): void {
  URL.revokeObjectURL(source.url);
  source.image.close();
}

export async function exportAvatarCrop(source: AvatarCropSource, area: Area): Promise<File> {
  const canvas = document.createElement('canvas');
  // Match the existing stored avatar's display resolution. No full-resolution
  // crop or original metadata is sent to the local service.
  canvas.width = canvas.height = 256;
  const context = canvas.getContext('2d');
  if (!context) throw new Error('当前浏览器无法处理图片。');
  context.drawImage(source.image, area.x, area.y, area.width, area.height, 0, 0, 256, 256);
  return new File([await canvasBlob(canvas)], 'avatar.png', { type: 'image/png' });
}
