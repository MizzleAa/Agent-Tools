// 사내 API 에서 받은 이미지 (show_images) — 답변 아래 썸네일 격자, 누르면 크게 보기.
// 이미지는 관리 서버가 실행 폴더에 받아 둔 파일을 주소(/api/v1/runs/{id}/images/{n}/…)로 받는다. 인증 정보는 주소에 없다.
// 받지 못한 장은 이미지 자리에 '이미지를 찾을 수 없음'과 이유를 그린다 (정상 이미지처럼 보이지 않게).
import { useState } from 'react';
import { Modal } from '@/ui/Modal';
import { Icon } from '@/ui/Icon';
import { type ImageEntry, imageUrl } from './turnModel';

const PAGE = 12;   // 한 번에 그리는 썸네일 수 (많으면 '더 보기')

export function ImageGallery({ runId, images }: { runId: string; images: ImageEntry[] }) {
  const [shown, setShown] = useState(PAGE);
  const [open, setOpen] = useState<ImageEntry | null>(null);
  if (!images.length) return null;
  const ok = images.filter((im) => im.ok).length;
  return (
    <div className="img-gallery">
      <div className="img-gallery-head">
        <Icon name="image" size={14} />
        <span>이미지 {images.length}장{ok < images.length ? ` · 표시 ${ok} · 실패 ${images.length - ok}` : ''}</span>
        <span className="muted small">사내 API 에서 받은 그대로 · 누르면 크게</span>
      </div>
      <div className="img-grid">
        {images.slice(0, shown).map((im, i) => (
          <ImageCard key={`${im.id}-${i}`} runId={runId} image={im} index={i + 1} onOpen={() => setOpen(im)} />
        ))}
      </div>
      {images.length > shown && (
        <button type="button" className="btn" onClick={() => setShown((n) => n + PAGE)}>
          {Math.min(PAGE, images.length - shown)}장 더 보기 (남은 {images.length - shown}장)
        </button>
      )}
      {open && <ImageViewer runId={runId} image={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

function ImageCard({ runId, image, index, onOpen }: { runId: string; image: ImageEntry; index: number; onOpen: () => void }) {
  const [broken, setBroken] = useState(false);
  const failed = !image.ok || broken;
  const reason = !image.ok ? image.reason : '저장해 둔 이미지를 불러오지 못함';
  return (
    <figure className={`img-card${failed ? ' failed' : ''}`} title={image.path}>
      {failed ? (
        <div className="img-missing" role="img" aria-label={`이미지를 찾을 수 없음: ${image.label}`}>
          <Icon name="x" size={22} />
          <b>이미지를 찾을 수 없음</b>
          <span>{reason}</span>
        </div>
      ) : (
        <button type="button" className="img-thumb" onClick={onOpen} aria-label={`${image.label} 크게 보기`}>
          <img src={imageUrl(runId, image.id, 'thumb')} alt={image.label} loading="lazy" decoding="async"
            onError={() => setBroken(true)} />
        </button>
      )}
      <figcaption>
        <span className="img-n">{index}</span>
        <span className="img-label">{image.label}</span>
        {!failed && image.width > 0 && <span className="img-size">{image.width}×{image.height} · {image.format}</span>}
      </figcaption>
    </figure>
  );
}

function ImageViewer({ runId, image, onClose }: { runId: string; image: ImageEntry; onClose: () => void }) {
  const [loaded, setLoaded] = useState(false);
  const [broken, setBroken] = useState(false);
  return (
    <Modal onClose={onClose} wide labelledBy="img-view-title">
      <div className="sheet-head">
        <h2 className="modal-title img-view-title" id="img-view-title">{image.label}</h2>
        <a className="mini-btn" href={imageUrl(runId, image.id, 'original')} target="_blank" rel="noreferrer"
          title="받은 원본 그대로 (새 탭)">원본</a>
        <button type="button" className="icon-btn" aria-label="닫기" onClick={onClose}><Icon name="x" /></button>
      </div>
      <div className="img-view">
        {broken ? (
          <div className="img-missing"><Icon name="x" size={22} /><b>이미지를 불러오지 못함</b></div>
        ) : (
          <>
            {!loaded && <span className="muted small">불러오는 중…</span>}
            <img src={imageUrl(runId, image.id, 'view')} alt={image.label} onLoad={() => setLoaded(true)}
              onError={() => setBroken(true)} style={loaded ? undefined : { visibility: 'hidden' }} />
          </>
        )}
      </div>
      <div className="img-view-meta muted small">
        {image.width > 0 && <span>{image.width}×{image.height} · {image.format} · {(image.bytes / 1024).toFixed(0)}KB</span>}
        <span className="img-path">{image.api} {image.path}</span>
      </div>
    </Modal>
  );
}
