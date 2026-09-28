'use client';
import { createContext } from 'react';
export const FileLinkContext = createContext<{
  open: (path: string, opener: HTMLElement, base?: string) => void;
  basePreview?: string;
  imagesUrl?: string;
} | undefined>(undefined);
