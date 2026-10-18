export function BrandMark({ compact = false }: { compact?: boolean }) {
  const size = compact ? 46 : 48;

  return (
    // Local static asset: the standalone app has no Next image service.
    <img
      className={`brand-mark${compact ? ' is-compact' : ''}`}
      src="/assets/pulsara-icon.png"
      width={size}
      height={size}
      alt=""
      aria-hidden="true"
      draggable={false}
    />
  );
}
