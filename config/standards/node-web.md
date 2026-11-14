# JavaScript / TypeScript 규격 (Node.js 24, 브라우저 웹 앱)

## 환경 확인
Node·npm 버전과, 프로젝트에 이미 설치된 패키지를 확인하고 보고에 적는다.
```
node --version
npm --version
npm ls --depth=0        # 프로젝트 폴더 안에서
```

## 프로젝트 구성
**브라우저 웹 앱**
```
<project>/
  README.md
  package.json          # 테스트·스크립트용. 런타임 의존성이 없어도 둔다
  index.html
  src/                  # 로직과 화면 코드를 나눈다
  styles/
  tests/*.test.js
  .gitignore            # node_modules/
```
**Node 프로그램** (CLI·서버)
```
<project>/
  README.md
  package.json          # "scripts": { "start", "test" }
  package-lock.json
  src/
  tests/*.test.js
  .gitignore            # node_modules/
```
- TypeScript를 쓰면 `tsconfig.json`을 두고, 빌드 결과물(`dist/`)은 `.gitignore`에 넣는다.

## 의존성
- 패키지는 `package.json`과 `package-lock.json`에 버전과 함께 기록하고, 프로젝트 로컬 `node_modules/`에 설치한다.
- **금지**:
  - 전역 설치 `npm install -g`
  - 실행할 때 인터넷이 필요한 참조. 예: CDN `<script src="https://...">`, 실행할 때마다 패키지를 내려받는 명령

## 판정 명령
| 단계 | 명령 |
|---|---|
| V1 ① 문법 | `node --check <파일>.js` (TS면 `npx tsc --noEmit` 이 ①②를 함께 판정, 로컬 설치 기준) |
| V1 ② 정의되지 않은 이름 | `npx eslint src tests` (규칙 `no-undef` 켬, eslint 는 프로젝트 devDependencies) → 0건 |
| V1 ③ 모듈 로드 | 모든 모듈을 불러와 오류 0. 예: `node -e "Promise.all(['./src/a.js', …].map(p => import(p))).catch(e => { console.error(e); process.exit(1) })"` (브라우저 전용 모듈은 V4 로 명시) |
| V2 테스트 | `node --test` 또는 `npm test` |
| 실행 | README에 적은 명령 |

## 끝나지 않는 프로그램 (V3)
`00_공통.md` 4장을 따른다. 자동으로 확인할 수단이 없는 화면 동작은 V4(사람 확인 필요)로 명시한다.

## 정리 대상
`_scratch/`. `node_modules/`는 정리 대상이 아니고 `.gitignore`에 넣는다.
