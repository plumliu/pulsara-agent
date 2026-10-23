/** Shared presentation only; each tab keeps ownership of its loading state. */
export function WorkspaceLoading({ overlay = false }: { overlay?: boolean }) {
  return <div className={`workspace-loading${overlay ? ' workspace-loading--overlay' : ''}`} role="status">
    <span className="workspace-loading__spinner" aria-hidden="true" />
    <span>正在加载...</span>
  </div>;
}
