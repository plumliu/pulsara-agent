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
  run(binary: 'initdb' | 'pg_ctl', args: readonly string[]): Promise<{stdout: string; stderr: string}>;
  removeDataDirectory(): Promise<void>;
}
