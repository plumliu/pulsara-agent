if (!Range.prototype.getClientRects) {
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
}

if (!Range.prototype.getBoundingClientRect) {
  Range.prototype.getBoundingClientRect = () => new DOMRect();
}

if (!document.elementFromPoint) {
  document.elementFromPoint = () => document.querySelector('[contenteditable="true"]');
}

let objectUrlSequence = 0;
if (!URL.createObjectURL) {
  URL.createObjectURL = () => `blob:pulsara-test:${objectUrlSequence += 1}`;
}
if (!URL.revokeObjectURL) {
  URL.revokeObjectURL = () => undefined;
}
