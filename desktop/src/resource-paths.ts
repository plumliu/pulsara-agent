import path from 'node:path';

/** Paths inside the existing local desktop resource bundle, without I/O or state. */
export interface DesktopResourcePaths {
  readonly root: string;
  readonly pythonExecutable: string;
  readonly playwrightBrowsers: string;
  readonly postgresBin: string;
  readonly packageCli: string;
}

export function desktopResourcePaths(root: string): DesktopResourcePaths {
  return {
    root,
    pythonExecutable: path.join(root, 'python', 'bin', 'python3'),
    playwrightBrowsers: path.join(root, 'playwright'),
    postgresBin: path.join(root, 'postgres', 'bin'),
    packageCli: path.join(root, 'bin', 'pulsara'),
  };
}
