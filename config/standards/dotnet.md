# C# / .NET 규격

## 환경 확인
설치된 SDK 목록과 쓸 수 있는 템플릿을 확인하고, 고른 대상 프레임워크를 보고에 적는다.
```
dotnet --list-sdks
dotnet new list
```

## 프로젝트 구성
```
<Name>/
  README.md
  <Name>.sln
  src/<Name>/<Name>.csproj                 # 앱
  src/<Name>.Core/<Name>.Core.csproj       # UI 없는 로직 (테스트 대상). 작은 앱은 생략 가능
  tests/<Name>.Tests/<Name>.Tests.csproj   # 자동 테스트 프로젝트
  .gitignore                               # bin/  obj/
```
- `bin/`·`obj/`·`*_wpftmp.*`는 **정상 빌드 산출물**이다. 정리 대상이 아니고, `.gitignore`에 넣는다.

## 의존성
- NuGet 패키지는 csproj의 `PackageReference`로만 선언한다. 버전을 명시하고 프로젝트 로컬로 복원한다.
- **금지**: 전역 도구 설치(`dotnet tool install -g`), 시스템 전역 런타임·구성요소 설치
- 폐쇄망 반입을 위해 쓰는 패키지 목록을 보고에 적는다.

## 판정 명령
| 단계 | 명령 |
|---|---|
| V1 빌드 | `dotnet build` → 오류 0 (경고 수는 보고). 컴파일러가 V1 ①문법 ②정의되지 않은 이름 ③참조 로드를 함께 판정 |
| V2 테스트 | `dotnet test` |
| 실행 | `dotnet run --project src/<Name>` (README에 적은 명령) |

## 끝나지 않는 프로그램 (V3)
`00_공통.md` 4장을 따른다. GUI 앱도 사람 손 없이 핵심 시나리오를 수행하고 스스로 종료하는 확인 경로가 있어야 한다. 결과는 출력, 결과 파일, exit code 중 하나 이상으로 남긴다.

## 정리 대상
`_scratch/`, 쓰지 않는 템플릿 기본 파일
