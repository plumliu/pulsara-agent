// Live, self-contained visualization playback only. The browser still owns
// task execution and Animation objects. This is not a JS/process freeze; see
// archived_docs/PULSARA_HTML_PLAYBACK_LIFECYCLE_SPEC.zh.md for the deliberately narrow scope.
export default function installVisualizationPlayback(initialVisible) {
  const nativeNow = performance.now.bind(performance);
  const requestFrame = requestAnimationFrame.bind(window);
  const cancelFrame = cancelAnimationFrame.bind(window);
  const timeout = setTimeout.bind(window);
  const clearTimer = clearTimeout.bind(window);
  let visible = Boolean(initialVisible);
  let pausedAt = visible ? undefined : nativeNow();
  let hiddenTime = 0;
  const now = () => (pausedAt ?? nativeNow()) - hiddenTime;
  // Keep rAF and performance.now in the same activity-time domain. Date and
  // the rest of the platform's wall-clock/timeline APIs deliberately stay native.
  Object.defineProperty(performance, 'now', { configurable: true, value: now });

  const frames = new Map();
  const timers = new Map();
  let nextFrame = 0, nextTimer = 0;
  let lastNativeFrame, lastFrameTime = 0;
  function scheduleFrame(id, pending) {
    pending.native = requestFrame(timestamp => {
      pending.native = undefined;
      if (!visible || !frames.has(id)) return;
      frames.delete(id);
      if (timestamp !== lastNativeFrame) {
        lastNativeFrame = timestamp;
        lastFrameTime = Math.max(lastFrameTime, timestamp - hiddenTime);
      }
      // Separate native callbacks preserve browser exception isolation.
      pending.callback.call(window, lastFrameTime);
    });
  }
  window.requestAnimationFrame = callback => {
    if (typeof callback !== 'function') throw new TypeError('Animation callback must be a function');
    const id = ++nextFrame, pending = { callback, native: undefined };
    frames.set(id, pending);
    if (visible) scheduleFrame(id, pending);
    return id;
  };
  window.cancelAnimationFrame = id => {
    const pending = frames.get(Number(id));
    if (pending?.native !== undefined) cancelFrame(pending.native);
    frames.delete(Number(id));
  };

  function scheduleTimer(id, pending) {
    pending.native = timeout(() => {
      pending.native = undefined;
      if (!visible || !timers.has(id)) return;
      if (!pending.repeat) timers.delete(id);
      // Next interval is a single future wait, never a hidden-time backlog.
      pending.due = now() + pending.delay;
      try { pending.callback.apply(window, pending.args); }
      finally {
        if (pending.repeat && timers.has(id) && visible) scheduleTimer(id, pending);
      }
    }, Math.max(0, pending.due - now()));
  }
  function createTimer(callback, delay, args, repeat) {
    // The existing script-src forbids string evaluation. Delegate that check to
    // Chromium (which returns 0); never introduce eval or an unsafe-eval policy.
    if (typeof callback !== 'function') return timeout(callback, delay, ...args);
    // HTML's signed-long delay conversion, not a new product duration limit.
    const milliseconds = Math.max(0, Number(delay) | 0);
    const id = ++nextTimer;
    const pending = { callback, args, repeat, delay: milliseconds, due: now() + milliseconds, native: undefined };
    timers.set(id, pending);
    if (visible) scheduleTimer(id, pending);
    return id;
  }
  window.setTimeout = (callback, delay = 0, ...args) => createTimer(callback, delay, args, false);
  window.setInterval = (callback, delay = 0, ...args) => createTimer(callback, delay, args, true);
  window.clearTimeout = window.clearInterval = id => {
    const pending = timers.get(Number(id));
    if (pending?.native !== undefined) clearTimer(pending.native);
    timers.delete(Number(id));
  };

  const heldAnimations = new Set();
  const heldCssAnimations = new Set();
  // CSSAnimation.pause/play permanently override CSS animation-play-state.
  // Hold its native rate instead, so a later CSS pause still belongs to CSS.
  // updatePlaybackRate/reverse can leave a pending rate that the native getter
  // does not expose yet; retain only the author's requested value for that case.
  const cssRates = new WeakMap();
  const animationPrototype = Animation.prototype;
  const play = animationPrototype.play, pause = animationPrototype.pause;
  const updateRate = animationPrototype.updatePlaybackRate;
  const rateProperty = Object.getOwnPropertyDescriptor(animationPrototype, 'playbackRate');
  const cssRate = animation => cssRates.has(animation) ? cssRates.get(animation) : rateProperty.get.call(animation);
  function setCssRate(animation, rate) {
    // updatePlaybackRate waits for a rendering opportunity. Offscreen frames
    // may not get one, so commit the rate synchronously and preserve the phase.
    const currentTime = animation.currentTime;
    rateProperty.set.call(animation, rate);
    if (currentTime !== null) animation.currentTime = currentTime;
  }
  function settlePause(animation, currentTime) {
    // Native pause also waits for a frame; setting currentTime completes its
    // pending pause now. Otherwise an offscreen animation keeps advancing.
    const holdTime = currentTime ?? animation.currentTime;
    if (holdTime !== null) animation.currentTime = holdTime;
  }
  function holdAnimation(animation) {
    // Scroll/custom timelines follow their own owner; don't convert them into
    // time-based playback. CSS and ordinary WAAPI use DocumentTimeline.
    if (visible || !(animation.timeline instanceof DocumentTimeline)
        || animation.playState !== 'running') return;
    if (animation instanceof CSSAnimation) {
      if (!cssRates.has(animation)) cssRates.set(animation, rateProperty.get.call(animation));
      setCssRate(animation, 0);
      heldCssAnimations.add(animation);
      return;
    }
    const currentTime = animation.currentTime;
    pause.call(animation);
    settlePause(animation, currentTime);
    heldAnimations.add(animation);
  }
  Object.defineProperty(animationPrototype, 'playbackRate', {
    ...rateProperty,
    set(value) {
      rateProperty.set.call(this, value);
      if (this instanceof CSSAnimation) {
        cssRates.set(this, rateProperty.get.call(this));
        holdAnimation(this);
      }
    },
  });
  animationPrototype.updatePlaybackRate = function (value) {
    if (!(this instanceof CSSAnimation)) return updateRate.call(this, value);
    // Convert once, preserving the native finite-double validation below.
    const rate = +value;
    const result = updateRate.call(this, rate);
    cssRates.set(this, rate);
    holdAnimation(this);
    return result;
  };
  const animate = Element.prototype.animate;
  Element.prototype.animate = function (...args) {
    const animation = animate.apply(this, args);
    holdAnimation(animation);
    return animation;
  };
  for (const name of ['play', 'reverse']) {
    const method = animationPrototype[name];
    animationPrototype[name] = function (...args) {
      const css = this instanceof CSSAnimation;
      const rate = css ? cssRate(this) : undefined;
      // reverse must reverse the author's rate, not the host's temporary zero.
      if (heldCssAnimations.has(this)) setCssRate(this, rate);
      let result;
      try { result = method.apply(this, args); }
      catch (error) { holdAnimation(this); throw error; }
      if (css) cssRates.set(this, name === 'reverse' ? -rate : rate);
      holdAnimation(this);
      return result;
    };
  }
  for (const name of ['pause', 'cancel', 'finish']) {
    const method = animationPrototype[name];
    animationPrototype[name] = function (...args) {
      // Restore rate before finish (endpoint/direction) or author pause/cancel.
      if (heldCssAnimations.has(this)) setCssRate(this, cssRate(this));
      const currentTime = !visible && name === 'pause' ? this.currentTime : null;
      let result;
      try { result = method.apply(this, args); }
      catch (error) { holdAnimation(this); throw error; }
      if (!visible && name === 'pause' && this.timeline instanceof DocumentTimeline) settlePause(this, currentTime);
      heldAnimations.delete(this); // The author's latest intent owns this object.
      heldCssAnimations.delete(this);
      return result;
    };
  }

  const heldSvg = new Set();
  const svgPrototype = SVGSVGElement.prototype;
  const pauseSvg = svgPrototype.pauseAnimations, resumeSvg = svgPrototype.unpauseAnimations;
  function holdSvg(svg) {
    if (!visible && !svg.animationsPaused()) {
      pauseSvg.call(svg);
      heldSvg.add(svg);
    }
  }
  svgPrototype.pauseAnimations = function () {
    heldSvg.delete(this);
    return pauseSvg.call(this);
  };
  svgPrototype.unpauseAnimations = function () {
    const result = resumeSvg.call(this);
    holdSvg(this);
    return result;
  };
  function collectAnimations() {
    if (visible) return;
    for (const animation of document.getAnimations()) holdAnimation(animation);
    for (const svg of document.querySelectorAll('svg')) holdSvg(svg);
    // A detached element can be reinserted. Keep host-held objects until resume;
    // detachment alone is not author cancellation and must not strand a pause.
    for (const animation of heldAnimations) {
      if (animation.playState === 'idle' || animation.playState === 'finished') heldAnimations.delete(animation);
    }
    for (const animation of heldCssAnimations) {
      if (animation.playState === 'idle' || animation.playState === 'finished') {
        setCssRate(animation, cssRate(animation));
        heldCssAnimations.delete(animation);
      }
    }
  }
  const changes = new MutationObserver(collectAnimations);
  function observeAnimations() {
    changes.observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
    collectAnimations();
  }
  document.addEventListener('animationstart', collectAnimations, true);
  document.addEventListener('transitionrun', collectAnimations, true);
  document.addEventListener('DOMContentLoaded', collectAnimations);
  if (!visible) observeAnimations();

  return function setVisible(value) {
    const next = Boolean(value);
    if (next === visible) return;
    if (!next) {
      pausedAt = nativeNow();
      visible = false;
      for (const pending of frames.values()) {
        if (pending.native !== undefined) cancelFrame(pending.native);
        pending.native = undefined;
      }
      for (const pending of timers.values()) {
        if (pending.native !== undefined) clearTimer(pending.native);
        pending.native = undefined;
      }
      observeAnimations();
      return;
    }
    hiddenTime += nativeNow() - pausedAt;
    pausedAt = undefined;
    visible = true;
    changes.disconnect();
    for (const animation of heldAnimations) {
      if (animation.playState === 'paused') play.call(animation);
    }
    heldAnimations.clear();
    // Restoring rate does not change the CSS/author-owned play state.
    for (const animation of heldCssAnimations) setCssRate(animation, cssRate(animation));
    heldCssAnimations.clear();
    for (const svg of heldSvg) if (svg.animationsPaused()) resumeSvg.call(svg);
    heldSvg.clear();
    for (const [id, pending] of frames) if (pending.native === undefined) scheduleFrame(id, pending);
    for (const [id, pending] of timers) if (pending.native === undefined) scheduleTimer(id, pending);
  };
}
