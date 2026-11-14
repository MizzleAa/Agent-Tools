const PATHS = {
  chevron: 'M8 5.5l4.5 4.5L8 14.5',
  check: 'M4.5 10.5l3.5 3.5 7.5-8',
  x: 'M6 6l8 8M14 6l-8 8',
  copy: 'M7.5 7.5h8v8h-8zM4.5 12.5v-8h8',
  hand: 'M10 3.5v7M6.5 6v6.5a3.5 3.5 0 007 0V8M13.5 8V5.5',
  trash: 'M4.5 6h11M8 6V4.5h4V6M6 6l.7 9.5h6.6L14 6M8.7 9v4M11.3 9v4',
  folder: 'M3 6.5V15h14V8H9.5L8 6H3.5',
  plus: 'M10 4v12M4 10h12',
  menu: 'M3 5.5h14M3 10h14M3 14.5h14',
  send: 'M10 15.5V4.5M5 9.5l5-5 5 5',
  image: 'M3.5 4.5h13v11h-13zM3.5 13l4-4 3.5 3.5 2-2 3.5 3.5M12.5 8a.01.01 0 100 .02',
  // Claude Code 탭 (관리)
  terminal: 'M3.5 4.5h13v11h-13zM6.5 8l2.5 2-2.5 2M10.5 12.5h3',
  file: 'M5.5 3.5h6l3 3v10h-9zM11.5 3.5v3h3',
  edit: 'M4 16l1-4 8-8 3 3-8 8zM11.5 5.5l3 3',
  search: 'M8.5 14a5.5 5.5 0 100-11 5.5 5.5 0 000 11zM12.5 12.5l4 4',
  globe: 'M10 16.5a6.5 6.5 0 100-13 6.5 6.5 0 000 13zM3.5 10h13M10 3.5c2 2 2 11 0 13M10 3.5c-2 2-2 11 0 13',
  list: 'M7.5 5.5h9M7.5 10h9M7.5 14.5h9M3.5 5.5h.5M3.5 10h.5M3.5 14.5h.5',
  bot: 'M5 7.5h10v8H5zM10 4.5v3M7.8 11h.4M11.8 11h.4',
  help: 'M10 16.5a6.5 6.5 0 100-13 6.5 6.5 0 000 13zM8 8a2 2 0 113 1.7c-.7.4-1 .8-1 1.6M10 13.5h.01',
  shield: 'M10 3l5.5 2v4.5c0 3.5-2.5 6-5.5 7.5-3-1.5-5.5-4-5.5-7.5V5z',
  stop: 'M6 6h8v8H6z',
  refresh: 'M15.5 10a5.5 5.5 0 11-1.6-3.9M15.5 4v3.5H12',
  gear: 'M10 12.5a2.5 2.5 0 100-5 2.5 2.5 0 000 5zM10 3v2M10 15v2M3 10h2M15 10h2M5 5l1.4 1.4M13.6 13.6L15 15M5 15l1.4-1.4M13.6 6.4L15 5',
  activity: 'M3 10h3l2-5 4 10 2-5h3',
  paperclip: 'M14.5 9.5l-5 5a3 3 0 01-4.2-4.2l6-6a2 2 0 012.8 2.8l-6 6a1 1 0 01-1.4-1.4l5-5',
  arrowDown: 'M10 4.5v11M5 10.5l5 5 5-5',
  play: 'M6.5 4.5l9 5.5-9 5.5z',
  download: 'M10 4.5v9M6 9.5l4 4 4-4M4 15.5h12',
  alert: 'M10 3.5l7 12.5H3zM10 8.5v3.5M10 14h.01',
  info: 'M10 16.5a6.5 6.5 0 100-13 6.5 6.5 0 000 13zM10 9v4.5M10 6.5h.01',
  plug: 'M7 3.5v4M13 3.5v4M5.5 7.5h9v2a4.5 4.5 0 01-9 0zM10 14v3',
  dots: 'M10 16.5a6.5 6.5 0 100-13 6.5 6.5 0 000 13zM7 10h.01M10 10h.01M13 10h.01',
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 16, className }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg viewBox="0 0 20 20" width={size} height={size} className={className} aria-hidden="true">
      <path d={PATHS[name]} fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function Spinner() {
  return <span className="spinner" />;
}
