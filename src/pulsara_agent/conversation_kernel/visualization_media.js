// Native media keeps its browser decoder and playback clock. Visibility only
// grants permission to play; becoming visible never resumes a paused video.
export default function installVisualizationMedia(initialVisible = false) {
  let visible = initialVisible, retired = false;
  const media = new Set();
  const nativePlay = HTMLMediaElement.prototype.play;
  const pause = element => element.pause();
  const release = element => {
    pause(element);
    element.removeAttribute('src');
    element.srcObject = null;
    for (const source of element.querySelectorAll('source')) source.removeAttribute('src');
    element.load();
    media.delete(element);
  };
  HTMLMediaElement.prototype.play = function (...args) {
    media.add(this);
    if (!visible || retired) {
      pause(this);
      return Promise.reject(new DOMException('Preview is hidden.', 'NotAllowedError'));
    }
    return nativePlay.apply(this, args);
  };
  const remember = node => {
    if (!(node instanceof Element)) return;
    const elements = [...node.querySelectorAll('video,audio')];
    if (node instanceof HTMLMediaElement) elements.push(node);
    for (const element of elements) {media.add(element); if (!visible) pause(element);}
  };
  // Native controls and autoplay do not have to call the JS play() method.
  document.addEventListener('play', event => {
    if (!(event.target instanceof HTMLMediaElement)) return;
    media.add(event.target);
    if (!visible || retired) pause(event.target);
  }, true);
  const observer = new MutationObserver(records => {
    for (const record of records) for (const node of record.addedNodes) remember(node);
    // Check after the mutation batch so synchronous reparenting retains state.
    for (const element of media) if (!element.isConnected) release(element);
  });
  observer.observe(document, {childList: true, subtree: true});
  remember(document.documentElement);
  addEventListener('pagehide', () => {
    retired = true; observer.disconnect();
    for (const element of media) release(element);
  }, {once: true});
  return value => {
    visible = value === true && !retired;
    if (!visible) for (const element of media) pause(element);
  };
}
