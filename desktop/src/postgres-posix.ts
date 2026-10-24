import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {mkdir, readFile, chmod, stat, realpath, rm} from 'node:fs/promises';
import path from 'node:path';
import type {PostgresOperations} from './postgres-operations';

const execute = promisify(execFile);

/** Node owns the existing local POSIX filesystem/process mechanism only. */
export function createPosixPostgresOperations(postgresBin: string, home: string): PostgresOperations {
  const directory = path.join(home, 'desktop', 'postgres');
  const socket = path.join(home, 'desktop', 'socket');
  const parent = path.dirname(directory);
  return {
    directory,
    socket,
    logPath: path.join(home, 'desktop', 'postgres.log'),
    socketAddressBytes: Buffer.byteLength(path.join(socket, '.s.PGSQL.5432')),
    async prepareLayout() {
      await mkdir(parent, {recursive: true, mode: 0o700});
      await mkdir(socket, {recursive: true, mode: 0o700});
      await chmod(socket, 0o700);
    },
    async readVersion() {
      return readFile(path.join(directory, 'PG_VERSION'), 'utf8');
    },
    async readInstance(directoryForm) {
      const lines = (await readFile(path.join(directory, 'postmaster.pid'), 'utf8')).split('\n');
      return {directory: directoryForm === 'resolved' ? path.resolve(lines[1] ?? '') : lines[1], socket: lines[4]};
    },
    async socketMode() {
      return (await stat(socket)).mode;
    },
    async resetPathsMatch() {
      return await realpath(directory) === directory && await realpath(parent) === parent;
    },
    async dataDirectoryInfo() {
      const info = await stat(directory);
      return {uid: info.uid, currentUserUid: process.getuid?.(), isDirectory: info.isDirectory()};
    },
    async run(binary, args) {
      return execute(path.join(postgresBin, binary), [...args], {
        env: {...process.env, LC_ALL: 'C'}, maxBuffer: 4 * 1024 * 1024,
      });
    },
    async removeDataDirectory() {
      await rm(directory, {recursive: true});
    },
  };
}
