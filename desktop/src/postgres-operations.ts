/** A command failure is settled only when its complete numeric exit is known. */
export interface PostgresCommandError extends Error {
  readonly postgresExecution: 'not-submitted' | 'unknown' | 'failed';
  readonly code?: string | number | null;
}

export function isSettledPostgresFailure(error: unknown): error is PostgresCommandError & {code: number} {
  if (!(error instanceof Error)) return false;
  const failure = error as Partial<PostgresCommandError>;
  return failure.postgresExecution === 'failed'
    && typeof failure.code === 'number' && Number.isInteger(failure.code) && failure.code > 0;
}

/** Physical observations and commands for one bundled PostgreSQL layout.
 * BundledPostgres alone decides lifecycle order and retains ownership state. */
export interface PostgresOperations {
  readonly directory: string;
  readonly socket: string;
  readonly logPath: string;
  readonly socketAddressBytes: number;
  prepareLayout(): Promise<void>;
  readVersion(): Promise<string>;
  readInstance(directoryForm: 'raw' | 'resolved'): Promise<{
    directory: string | undefined;
    socket: string | undefined;
  }>;
  socketMode(): Promise<number>;
  resetPathsMatch(): Promise<boolean>;
  dataDirectoryInfo(): Promise<{
    uid: number;
    currentUserUid: number | undefined;
    isDirectory: boolean;
  }>;
  /** Reject with PostgresCommandError, preserving the command's original error and output. */
  run(binary: 'initdb' | 'pg_ctl', args: readonly string[]): Promise<{stdout: string; stderr: string}>;
  removeDataDirectory(): Promise<void>;
}
