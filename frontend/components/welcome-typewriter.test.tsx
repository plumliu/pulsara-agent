import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  WELCOME_TYPEWRITER_PHRASES,
  WelcomeTypewriter,
  chooseWelcomePhrase,
} from './welcome-typewriter';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function runNextTimer() {
  act(() => vi.runOnlyPendingTimers());
}

const chineseInputCases = [
  {
    randomValue: 0,
    phrase: WELCOME_TYPEWRITER_PHRASES[0],
    segments: [
      { input: "you'shen'me", committed: '有什么' },
      { input: "xiang'zuo'de", committed: '想做的？' },
    ],
  },
  {
    randomValue: 0.2,
    phrase: WELCOME_TYPEWRITER_PHRASES[1],
    segments: [
      { input: "jin'tian", committed: '今天' },
      { input: "you'shen'me", committed: '有什么' },
      { input: "an'pai", committed: '安排？' },
    ],
  },
  {
    randomValue: 0.4,
    phrase: WELCOME_TYPEWRITER_PHRASES[2],
    segments: [
      { input: "zhun'bei", committed: '准备' },
      { input: "xian'gao'ding", committed: '先搞定' },
      { input: "na'yi'xiang", committed: '哪一项？' },
    ],
  },
] as const;

describe('WelcomeTypewriter', () => {
  it('chooses another language 80% of the time and excludes the current phrase', () => {
    const sequence = (...values: number[]) => {
      let index = 0;
      return () => values[index++];
    };

    expect(chooseWelcomePhrase(0, sequence(0.79, 0.99))).toBe(5);
    expect(chooseWelcomePhrase(0, sequence(0.8, 0))).toBe(1);
    expect(chooseWelcomePhrase(4, sequence(0.79, 0))).toBe(0);
    expect(chooseWelcomePhrase(4, sequence(0.8, 0.99))).toBe(5);
  });

  it.each(chineseInputCases)('composes $phrase through visible pinyin input', ({
    randomValue, phrase, segments,
  }) => {
    vi.useFakeTimers();
    const view = render(<WelcomeTypewriter active random={() => randomValue} />);
    const line = view.container.querySelector('.welcome-typewriter__line > span:first-child')!;

    runNextTimer();
    expect(screen.getByRole('heading', { name: phrase })).toBeTruthy();
    runNextTimer();
    let committed = '';
    for (const segment of segments) {
      const input = Array.from(segment.input);
      for (let index = 0; index < input.length; index += 1) {
        runNextTimer();
        expect(line.textContent).toBe(committed + input.slice(0, index + 1).join(''));
      }
      runNextTimer();
      committed += segment.committed;
      expect(line.textContent).toBe(committed);
    }
    expect(line.textContent).toBe(phrase);
  });

  it('blinks the underscore three times, deletes, and chooses another random phrase', () => {
    vi.useFakeTimers();
    const random = vi.fn(() => 0.5);
    const view = render(<WelcomeTypewriter active random={random} />);
    const line = () => view.container.querySelector('.welcome-typewriter__line > span:first-child')!;
    const cursor = () => view.container.querySelector('.welcome-typewriter__cursor')!;
    const phrase = WELCOME_TYPEWRITER_PHRASES[3];

    runNextTimer();
    runNextTimer();
    for (const character of Array.from(phrase)) {
      runNextTimer();
      expect(line().textContent?.endsWith(character)).toBe(true);
    }
    expect(line().textContent).toBe(phrase);
    expect(cursor().textContent).toBe('_');

    runNextTimer();
    for (let cycle = 0; cycle < 3; cycle += 1) {
      runNextTimer();
      expect(cursor().classList.contains('is-hidden')).toBe(true);
      runNextTimer();
      expect(cursor().classList.contains('is-hidden')).toBe(false);
    }

    runNextTimer();
    act(() => vi.advanceTimersByTime(41));
    expect(line().textContent).toBe(phrase);
    act(() => vi.advanceTimersByTime(1));
    expect(line().textContent).toBe(phrase.slice(0, -1));
    for (let index = 1; index < Array.from(phrase).length; index += 1) runNextTimer();
    expect(line().textContent).toBe('');
    runNextTimer();
    runNextTimer();
    expect(screen.queryByRole('heading', { name: phrase })).toBeNull();
  });

  it('finishes the current input-method composition without jumping or cycling', () => {
    vi.useFakeTimers();
    const random = vi.fn(() => 0);
    const view = render(<WelcomeTypewriter active random={random} />);
    const line = view.container.querySelector('.welcome-typewriter__line > span:first-child')!;
    runNextTimer();
    runNextTimer();
    runNextTimer();
    expect(line.textContent).toBe('y');

    view.rerender(<WelcomeTypewriter active cycling={false} random={random} />);
    expect(line.textContent).toBe('y');
    for (let guard = 0; vi.getTimerCount() > 0 && guard < 100; guard += 1) runNextTimer();
    expect(line.textContent).toBe(WELCOME_TYPEWRITER_PHRASES[0]);
    expect(view.container.querySelector('.welcome-typewriter__cursor')?.classList.contains('is-hidden'))
      .toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });
});
