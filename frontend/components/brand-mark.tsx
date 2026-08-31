export function BrandMark({ compact = false }: { compact?: boolean }) {
  return (
    <span className={`brand-mark${compact ? ' is-compact' : ''}`} aria-hidden="true">
      <span className="brand-mark__orbit" />
      <span className="brand-mark__star" />
    </span>
  );
}
