'use client';

import { useEffect, useMemo, useState } from 'react';

export const WELCOME_TYPEWRITER_PHRASES = [
  '有什么想做的？',
  '今天有什么安排？',
  '准备先搞定哪一项？',
  'Ready when you are.',
  'Feel free to vibe with me.',
  'Here for your workflow.',
] as const;

type TypewriterPhase = 'arrival' | 'typing' | 'blinking' | 'deleting' | 'gap';
type ChineseInputSegment = { input: string; committed: string };
type TypingFrame = { text: string; kind: 'input' | 'commit'; character?: string };

const INITIAL_DELAY_MS = 360;
const BLINK_HALF_CYCLE_MS = 420;
const CHINESE_DELETE_DELAY_MS = 52;
const ENGLISH_DELETE_DELAY_MS = 42;
const BETWEEN_PHRASES_MS = 300;
const IME_COMMIT_DELAY_MS = 135;

const CHINESE_INPUT_SEGMENTS: ReadonlyArray<ReadonlyArray<ChineseInputSegment>> = [
  [
    { input: "you'shen'me", committed: '有什么' },
    { input: "xiang'zuo'de", committed: '想做的？' },
  ],
  [
    { input: "jin'tian", committed: '今天' },
    { input: "you'shen'me", committed: '有什么' },
    { input: "an'pai", committed: '安排？' },
  ],
  [
    { input: "zhun'bei", committed: '准备' },
    { input: "xian'gao'ding", committed: '先搞定' },
    { input: "na'yi'xiang", committed: '哪一项？' },
  ],
];

function splitGraphemes(value: string): string[] {
  if (typeof Intl.Segmenter !== 'function') return Array.from(value);
  const segmenter = new Intl.Segmenter(undefined, { granularity: 'grapheme' });
  return Array.from(segmenter.segment(value), item => item.segment);
}

export function chooseWelcomePhrase(previous: number | null, random: () => number): number {
  const candidates = WELCOME_TYPEWRITER_PHRASES.map((_, index) => index)
    .filter(index => index !== previous);
  if (previous === null) {
    return candidates[Math.min(candidates.length - 1, Math.floor(random() * candidates.length))];
  }
  const previousIsChinese = previous < 3;
  const chooseDifferentLanguage = random() < 0.8;
  const languageCandidates = candidates.filter(index => (
    (index < 3) !== previousIsChinese
  ) === chooseDifferentLanguage);
  return languageCandidates[Math.min(
    languageCandidates.length - 1,
    Math.floor(random() * languageCandidates.length),
  )];
}

function typingDelay(character: string, random: () => number): number {
  if (/\s/u.test(character)) return 42 + Math.floor(random() * 22);
  if (/[,.?!，。？！]/u.test(character)) return 125 + Math.floor(random() * 36);
  if (/\p{Script=Han}/u.test(character)) return 105 + Math.floor(random() * 42);
  return 54 + Math.floor(random() * 34);
}

function typingFrames(phraseIndex: number, phrase: string): TypingFrame[] {
  const segments = CHINESE_INPUT_SEGMENTS[phraseIndex];
  if (!segments) {
    return Array.from(phrase).map((character, index) => ({
      text: Array.from(phrase).slice(0, index + 1).join(''),
      kind: 'input',
      character,
    }));
  }
  const frames: TypingFrame[] = [];
  let committedPrefix = '';
  for (const segment of segments) {
    const input = Array.from(segment.input);
    input.forEach((character, index) => frames.push({
      text: committedPrefix + input.slice(0, index + 1).join(''),
      kind: 'input',
      character,
    }));
    committedPrefix += segment.committed;
    frames.push({ text: committedPrefix, kind: 'commit' });
  }
  return frames;
}

export function WelcomeTypewriter({
  active,
  cycling = true,
  random = Math.random,
}: {
  active: boolean;
  cycling?: boolean;
  random?: () => number;
}) {
  const [phraseIndex, setPhraseIndex] = useState<number | null>(null);
  const [typingStep, setTypingStep] = useState(0);
  const [visibleCount, setVisibleCount] = useState(0);
  const [phase, setPhase] = useState<TypewriterPhase>('arrival');
  const [blinkHalfCycles, setBlinkHalfCycles] = useState(0);
  const [cursorVisible, setCursorVisible] = useState(true);
  const phrase = WELCOME_TYPEWRITER_PHRASES[phraseIndex ?? 0];
  const graphemes = useMemo(() => splitGraphemes(phrase), [phrase]);
  const frames = useMemo(() => typingFrames(phraseIndex ?? 0, phrase), [phrase, phraseIndex]);

  useEffect(() => {
    if (!active) return;

    let delay = 0;
    let advance: () => void;
    if (phraseIndex === null) {
      advance = () => setPhraseIndex(chooseWelcomePhrase(null, random));
    } else if (!cycling) {
      if (phase === 'arrival' || phase === 'typing') {
        if (typingStep >= frames.length) return;
        const frame = frames[typingStep];
        delay = frame.kind === 'commit'
          ? IME_COMMIT_DELAY_MS
          : typingDelay(frame.character ?? '', random);
        advance = () => setTypingStep(step => step + 1);
      } else {
        if (visibleCount >= graphemes.length) return;
        delay = typingDelay(graphemes[visibleCount], random);
        advance = () => setVisibleCount(count => count + 1);
      }
    } else if (phase === 'arrival') {
      delay = INITIAL_DELAY_MS;
      advance = () => setPhase('typing');
    } else if (phase === 'typing') {
      if (typingStep >= frames.length) {
        advance = () => {
          setVisibleCount(graphemes.length);
          setBlinkHalfCycles(0);
          setCursorVisible(true);
          setPhase('blinking');
        };
      } else {
        const frame = frames[typingStep];
        delay = frame.kind === 'commit'
          ? IME_COMMIT_DELAY_MS
          : typingDelay(frame.character ?? '', random);
        advance = () => setTypingStep(step => step + 1);
      }
    } else if (phase === 'blinking') {
      if (blinkHalfCycles >= 6) {
        advance = () => {
          setCursorVisible(true);
          setPhase('deleting');
        };
      } else {
        delay = BLINK_HALF_CYCLE_MS;
        advance = () => {
          setCursorVisible(visible => !visible);
          setBlinkHalfCycles(count => count + 1);
        };
      }
    } else if (phase === 'deleting') {
      if (visibleCount <= 0) {
        advance = () => setPhase('gap');
      } else {
      delay = phraseIndex < 3 ? CHINESE_DELETE_DELAY_MS : ENGLISH_DELETE_DELAY_MS;
        advance = () => setVisibleCount(count => Math.max(0, count - 1));
      }
    } else {
      delay = BETWEEN_PHRASES_MS;
      advance = () => {
        setPhraseIndex(current => chooseWelcomePhrase(current, random));
        setTypingStep(0);
        setVisibleCount(0);
        setCursorVisible(true);
        setPhase('typing');
      };
    }

    const timer = window.setTimeout(advance, delay);
    return () => window.clearTimeout(timer);
  }, [active, blinkHalfCycles, cycling, frames, graphemes, phase, phraseIndex,
    random, typingStep, visibleCount]);

  const displayedText = phase === 'arrival' || phase === 'typing'
    ? (frames[typingStep - 1]?.text ?? '')
    : graphemes.slice(0, visibleCount).join('');
  const showCursor = active && (!cycling || cursorVisible);

  return <h1 aria-label={phrase}>
    <span className="welcome-typewriter" aria-hidden="true">
      <span className="welcome-typewriter__sizer">{phrase}<span>_</span></span>
      <span className="welcome-typewriter__line">
        <span>{displayedText}</span>
        <span className={`welcome-typewriter__cursor${showCursor ? '' : ' is-hidden'}`}>_</span>
      </span>
    </span>
  </h1>;
}
