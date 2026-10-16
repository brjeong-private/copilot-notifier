# Copilot Notifier

GitHub Copilot 작업이 끝났을 때 **카카오톡 나와의 채팅 또는 Azure Communication
Services Email(ACS)** 로 완료 알림을 받기 위한 Windows 도구입니다.
추가 요약 모델이나 상시 실행 서버 없이, 로컬 완료 훅과 기존 응답 요약을 사용합니다.

## 현재 지원 범위

설정 예시와 목표 동작을 실제 구현 상태와 구분합니다.

| 기능 | 현재 구현 |
|---|---|
| Autopilot 완료 → 카카오톡 | 지원. 루트 작업의 수락된 완료 이벤트가 필요 |
| 일반 챗 / Plan 완료 알림 | 미지원. 현재는 알림을 보내지 않음 |
| Azure 이메일 | 인프라 배포 및 수동 테스트 스크립트만 지원 |
| 완료 훅 → 이메일 / 두 채널 동시 발송 | 미지원. 이메일 자동 대체 발송도 없음 |
| `.env` 로딩 및 채널·모드 선택 | 미지원. `.env.example`은 향후 연동할 설정 계약 |

**`.env`에 값을 입력하는 것만으로 현재 동작은 바뀌지 않습니다.**
아래 환경변수와 확장 메시지 형식은 향후 구현 기준이며, 현재 실행 방법은
[설치 및 실행](#설치-및-실행)을 따릅니다. 이 문서 변경으로 훅 동작을 확장하지 않습니다.

## 환경변수 설정 예시

[`.env.example`](.env.example)에 비밀값 없는 예시가 있습니다. 기존 `.env`가
없을 때만 복사하고 필요한 채널의 값을 채웁니다.

```powershell
if (-not (Test-Path -LiteralPath .\.env)) {
    Copy-Item -LiteralPath .\.env.example -Destination .\.env
}
```

`.env`는 PowerShell 스크립트가 아닙니다. 현재 자동 로더는 없으며 직접 실행하거나
`Invoke-Expression`으로 읽지 않습니다. `.env`, `.env.*`와 `.local`은 Git에서
제외하고 `.env.example`만 공유합니다. 실제 키·토큰·수신 주소를 커밋하거나
채팅·스크린샷·로그에 남기지 마세요.

### 공통 설정 계약 — 아직 미구현

| 변수 | 예시 / 기본 정책 | 의미 |
|---|---|---|
| `NOTIFIER_CHANNELS` | `kakao` | `kakao`, `azure_email`, `kakao,azure_email` 중 선택 |
| `NOTIFIER_NOTIFY_CHAT` | `false` | 일반 챗의 최종 응답 완료 알림 |
| `NOTIFIER_NOTIFY_PLAN` | `false` | 계획 작성 완료 알림. 구현 완료와 구분 |
| `NOTIFIER_NOTIFY_AUTOPILOT` | `true` | 수락된 Autopilot 목표 완료 알림 |

향후 두 채널을 선택하면 같은 완료 건을 각 채널에 한 번씩 전달합니다.
한 채널 실패가 다른 채널 성공으로 간주되어서는 안 되며, 별도로 상태를 기록합니다.
잘못된 채널·boolean 값이나 활성 채널의 필수 설정 누락은 명시적 설정 오류로
처리하고 다른 채널로 조용히 대체하지 않는 것을 구현 기준으로 합니다.

### 카카오톡

| 변수 | 입력할 값 / 현재 설정과의 관계 |
|---|---|
| `KAKAO_REST_API_KEY` | Kakao Developers 앱의 REST API 키. 현재는 연결 스크립트의 숨김 프롬프트에 입력 |
| `KAKAO_CLIENT_SECRET` | 카카오 로그인에서 활성화한 Client Secret. 현재는 숨김 프롬프트에 입력 |
| `KAKAO_REDIRECT_URI` | `http://localhost:53682/`. 현재 스크립트에 고정된 콜백 URI |
| `KAKAO_MESSAGE_URL` | 앱에 등록한 웹 도메인의 HTTPS 프로젝트 URL. 현재 `-MessageUrl` 인자에 전달 |

수신자는 OAuth로 로그인한 본인이므로 전화번호나 친구 ID는 필요하지 않습니다.
로그인 시 `talk_message` 동의가 필요합니다. 액세스·리프레시 토큰은
`.env`에 직접 관리하지 않고 현재의 연결 스크립트로 발급·갱신합니다.

### Azure 이메일

Azure 서비스는 **ACS Email**을 뜻하며 Microsoft Graph나 SMTP 설정이 아닙니다.
기존 스크립트의 Azure CLI / Microsoft Entra ID 인증 방식을 기준으로 하므로
연결 문자열이나 액세스 키 예시는 두지 않습니다.

| 변수 | 입력할 값 / 현재 설정과의 관계 |
|---|---|
| `AZURE_SUBSCRIPTION_ID` | Azure 구독 ID → 배포 인자 `-SubscriptionId`, 로컬 설정 `subscriptionId` |
| `AZURE_RESOURCE_GROUP` | 전용 리소스 그룹 → 배포 인자 `-ResourceGroup` |
| `AZURE_LOCATION` | 예: `westus3` → 배포 인자 `-Location`. 리소스 그룹 위치이며 이메일 데이터 위치와는 별개 |
| `AZURE_COMMUNICATION_ENDPOINT` | `https://YOUR-ACS-RESOURCE.communication.azure.com` → 로컬 설정 `endpoint` |
| `AZURE_EMAIL_SENDER` | 연결·검증된 이메일 도메인의 발신 주소 → 로컬 설정 `sender` |
| `AZURE_EMAIL_RECIPIENT` | 수신 이메일 주소 하나 → 로컬 설정 `recipient` |

현재 배포 스크립트는 엔드포인트와 발신 주소를 배포 결과에서 구해
`.local\config.json`에 저장하며, 환경변수 값은 읽지 않습니다.
Azure 관리 도메인 또는 검증된 사용자 도메인이 ACS 리소스에 연결되어야 하고,
로그인 계정에는 배포 및 이메일 발송에 필요한 Azure 권한이 있어야 합니다.
무인 환경용 서비스 주체·관리 ID 인증은 현재 스크립트에 구현되어 있지 않습니다.

## 작업별 알림 정책

아래 표의 기본값은 현재의 Autopilot 전용 동작을 유지합니다.
일반 챗·Plan의 활성화 옵션은 **향후 구현할 명세**이지 현재 지원 기능이 아닙니다.
`Stop` 훅이 발생했다는 이유만으로 작업 성공을 판단하지 않습니다.

| 작업 | 기본 알림 | 완료의 의미 / 향후 활성화 시 알림 시점 | 메시지 구분 |
|---|---|---|---|
| 일반 챗 / Interactive | 끔 | 사용자의 한 요청에 대한 루트 최종 응답이 끝났을 때 한 번. 중간 응답·도구 호출마다 보내지 않음 | `응답 완료` |
| Plan | 끔 | 계획 문서 또는 최종 계획 응답이 완성됐을 때 한 번. 코드를 구현했다는 의미가 아님 | `계획 작성 완료` |
| Autopilot | 켬 | 루트 `session.task_complete`가 수락된 완료일 때 한 번. 자동 반복·하위 에이전트 완료는 제외 | `작업 완료` |
| 하위 에이전트 / fleet 개별 작업 | 끔 | 개별 완료는 보내지 않고 상위 작업 완료에 포함 | 별도 알림 없음 |
| 권한·사용자 입력 대기 / 중간 진행 | 끔 | 완료가 아니므로 보내지 않음 | 별도 알림 없음 |
| 취소·중단·오류·거절·크래시 | 끔 | 성공 완료 알림을 보내지 않음. 실패 전용 알림은 별도 기능으로 남김 | 성공 메시지 없음 |

일반 챗·Plan은 해당 환경에서 모드와 최종 응답 완료를 신뢰할 수 있게 판별하는
어댑터가 추가된 뒤에만 지원합니다. 현재는 SDK JSONL의 명시적인
`session.mode_changed` 또는 `user.message.agentMode`가 `autopilot`임을 확인하고,
완료 이벤트의 실패·비완료 상태를 제외합니다. 모드를 추측하지 않습니다.

현재 훅은 세션 ID와 완료 이벤트 ID로 중복 발송을 방지합니다.
같은 세션의 새로운 사용자 요청은 별개의 작업이며, 자동 연속 실행이나
시스템 메시지·하위 에이전트 프롬프트는 새 요청으로 세지 않습니다.
향후 다중 채널에서도 이 완료 건 식별을 유지하되 전송 상태는 채널별로 관리합니다.

## 메시지 형식

### 현재 카카오톡 메시지

나와의 채팅에 다음 여섯 필드를 한국어 텍스트로 보내고, 프로젝트 링크 버튼을 제공합니다.

```text
프로젝트: copilot-notifier
시작: 2026-10-16 22:00:00 +09:00
완료: 2026-10-16 22:03:15 +09:00
소요: 00:03:15
요청 요약: 카카오톡 완료 알림 추가
결과 요약: 나와의 채팅 전송 구현
```

- 프로젝트는 Git 루트 폴더명이며 Git 밖에서는 작업 폴더명입니다.
- 시작은 가장 최근 사용자 요청의 이벤트 시각, 완료는 수락된 루트 완료 이벤트 시각입니다.
  소요는 대기 시간을 포함한 경과 시간이며 CPU 실행 시간이 아닙니다.
- 발송하는 Windows의 로컬 시간대와 UTC 오프셋을 표시합니다. 24시간이 넘으면
  소요 시간의 총 시간을 유지합니다. 시각이 없거나 잘못되면 경고하고 발송하지 않으며
  현재 시각으로 대체하지 않습니다.
- 카카오 기본 텍스트 템플릿의 **전체 200자 제한**에 맞춥니다. 프로젝트명은 최대
  20자, 요청·결과 요약은 남은 공간을 나누어 쓰고 잘린 부분은 `...`로 표시합니다.
- 요청은 안전한 첫 비어 있지 않은 줄, 결과는 완료 `summary`의 안전한 첫 두 줄을
  합쳐 사용합니다. 새로운 AI 요약을 생성하지 않습니다.

### 현재 수동 이메일 테스트

기존 이메일 코드는 제목과 일반 텍스트 본문을 사용하며, 시간 필드는 없습니다.
`Send-TestEmail.ps1`은 실제 완료 훅이 아닌 고정된 준비 확인 메시지를 보냅니다.
기존 템플릿의 구조는 다음과 같습니다.

```text
제목: [Copilot] {프로젝트} | {요청 요약}

Project: {프로젝트} | Task: {요청 요약}
Result: {결과 요약}
```

### 확장 시 공통 메시지 명세 — 아직 미구현

일반 챗·Plan·이메일 자동 발송이 추가되면 다음 정보를 공통으로 사용합니다.
프로젝트, 모드, 완료 구분, 시작·완료 시각과 소요 시간, 요청·결과 요약은 필수이며
프로젝트 링크는 설정된 경우에만 표시합니다. 이메일은 일반 텍스트로 보내고
대화 전문·코드·도구 출력·첨부 파일은 보내지 않습니다.

```text
이메일 제목: [Copilot][Plan][계획 작성 완료] copilot-notifier

프로젝트: copilot-notifier
모드: Plan
상태: 계획 작성 완료
시작: 2026-10-16 22:00:00 +09:00
완료: 2026-10-16 22:03:15 +09:00
소요: 00:03:15
요청 요약: 알림 채널 확장 계획
결과 요약: 카카오톡과 ACS 연동 단계 정리
링크: https://github.com/brjeong-private/copilot-notifier
```

카카오톡 확장 형식은 첫 줄에 `[Plan] 계획 작성 완료`처럼 모드와 완료 구분을
추가하고 기존 여섯 필드를 유지합니다. 링크는 버튼으로 표시하며,
첫 줄을 포함한 **전체 200자 제한**을 유지하도록 요청·결과 요약 예산을 다시 계산합니다.
일반 챗은 `[Chat] 응답 완료`, Autopilot은 `[Autopilot] 작업 완료`로 구분합니다.
현재 카카오 메시지에는 이 모드·상태 줄이 없습니다.

## 설치 및 실행

### 카카오톡 자동 완료 알림 — 현재 지원

Windows, PowerShell 7, Copilot과 본인의 Kakao Developers 앱이 필요합니다.
명령은 이 저장소 루트 `copilot-notifier`에서 실행합니다.

1. [Kakao Developers](https://developers.kakao.com/)에서 앱을 생성합니다.
2. 카카오 로그인 및 `talk_message` 동의 항목을 활성화합니다.
3. Redirect URI를 정확히 `http://localhost:53682/`로 등록합니다.
4. REST API 키를 확인하고 Client Secret을 활성화합니다.
5. 메시지 버튼용 웹 도메인(예: `https://github.com`)을 등록합니다.

```powershell
pwsh -NoProfile -File .\scripts\Test-Notifier.ps1
pwsh -NoProfile -File .\scripts\Connect-Kakao.ps1 `
  -MessageUrl 'https://github.com/brjeong-private/copilot-notifier'
pwsh -NoProfile -File .\scripts\Send-TestKakao.ps1
pwsh -NoProfile -File .\scripts\Install-Hook.ps1
```

연결 스크립트의 숨김 프롬프트에 키와 Secret을 입력하고 브라우저에서 로그인·동의를
완료합니다. 콜백은 localhost에만 바인딩되고 OAuth state를 검증하며 3분 후 만료됩니다.
테스트 메시지를 확인한 뒤 훅 설치 후 CLI 세션을 재시작하고 VS Code를 다시 로드합니다.
조직 정책이 훅과 카카오 API 사용을 허용해야 합니다.

인증 정보는 `.local\kakao.xml`에 현재 Windows 사용자용 DPAPI로 암호화되어 저장되며
다른 사용자·컴퓨터로 복사해 사용할 수 없습니다. 토큰 갱신과 리프레시 토큰 교체를
처리하지만, 동의 철회·리프레시 토큰 만료 시 연결 스크립트를 다시 실행해야 합니다.
발송 중 연결 스크립트를 실행하지 마세요. 공유 설정 잠금은 동시 갱신을 거부합니다.

### Azure 이메일 배포 및 수동 테스트 — 자동 완료 알림 아님

Azure CLI 설치와 `az login`이 필요합니다. 아래 명령은 실제 Azure 리소스를
생성하고 이메일을 발송하므로 비용·쿼터와 구독·수신 주소를 확인한 뒤 실행합니다.
카카오톡만 사용할 때는 Azure 배포가 필요하지 않습니다.

```powershell
az login
pwsh -NoProfile -File .\scripts\Deploy-Email.ps1 `
  -SubscriptionId 'YOUR-SUBSCRIPTION-ID' `
  -Recipient 'you@example.com' `
  -ResourceGroup 'rg-copilot-tools' `
  -Location 'westus3'
pwsh -NoProfile -File .\scripts\Send-TestEmail.ps1
```

`infra\email.json`은 Email Service, Azure 관리 도메인, 연결된 ACS 리소스를 배포합니다.
현재 템플릿의 데이터 위치는 `United States`입니다. 배포는 액세스 키를 저장하지 않고
`.local\config.json`을 생성하며, 발송 때 Azure CLI에서 Entra 액세스 토큰을 얻습니다.
기존 리소스를 자동 삭제하지 않습니다. 이 설정과 `.local\receipts`는
카카오 완료 훅에서 읽지 않습니다.

## 훅 범위와 전달 보장

Copilot CLI의 SDK JSONL 이벤트 기록을 지원합니다. VS Code Copilot harness는
같은 SDK 이벤트 기록을 훅에 제공할 때만 지원하므로 실제 세션에서 확인해야 합니다.
VS Code Local harness 기록은 Autopilot으로 취급하지 않습니다.

사용자 훅은 `%USERPROFILE%\.copilot\hooks` 또는 `%COPILOT_HOME%\hooks`의
`tools-copilot-notifier.json`에 설치됩니다. 다른 훅·저장소는 수정하지 않으며
다른 유틸리티가 소유한 동명 훅은 덮어쓰지 않습니다. 설치한 훅은 이 저장소의
스크립트 경로를 참조하므로 폴더를 옮겼다면 훅 경로도 확인해야 합니다.
로컬 Windows의 여러 저장소에 적용되지만 WSL·컨테이너·원격 확장 호스트·클라우드
에이전트는 별도 설정이 필요합니다. 훅은 에이전트에 추가 요약이나 작업을 요청하지 않습니다.

카카오 발송 기록은 Git에서 제외된 `.local\kakao-receipts`에 저장합니다.
기록에는 작업 식별자·상태·시각만 있고 메시지 본문이나 인증 정보는 없습니다.
`result_code: 0`만 성공으로 기록하며 이는 API 수락이지 읽음이나 휴대폰 푸시 보장이 아닙니다.
나에게 보내기는 다른 사람에게서 받은 메시지와 같은 푸시를 만들지 않을 수 있습니다.

카카오 API는 idempotency key나 전송 상태 조회를 제공하지 않습니다.
네트워크 결과가 불확실하면 `Pending`을 남기고 자동 재시도하지 않습니다.
중복을 피하는 대신 알림이 누락될 수 있습니다. 실패도 수동 확인이 필요하며,
채팅과 앱 설정을 확인한 뒤 특정 기록을 삭제해야 해당 완료 건의 재시도를 허용할 수 있습니다.
전송 실패는 훅 오류 출력으로 표시하고 Copilot에 추가 작업을 요구하지 않습니다.
이메일로 자동 대체하지 않습니다. API 쿼터와 계정 알림 설정도 적용됩니다.
ACS 수동 테스트의 성공 역시 발송 작업 성공이지 받은편지함 도착 확인은 아닙니다.

전체 경로·프롬프트·대화·소스·도구 인자·원시 이벤트는 첨부하지 않습니다.
코드 블록과 자격 증명처럼 보이는 줄을 제외하지만 **완전한 유출 방지 기능은 아닙니다.**
요청·결과의 짧은 요약도 외부 카카오/Azure로 전송해도 되는 작업에만 사용하세요.

설치 후 실제 Autopilot 작업을 완료해 여섯 필드를 확인하고, 일반 챗과 Plan에서는
메시지가 오지 않는지 확인합니다. 비활성화하려면 훅 디렉터리에서
`tools-copilot-notifier.json` 파일만 제거합니다.

## 참고

- [카카오 나에게 보내기 REST API](https://developers.kakao.com/docs/latest/en/kakaotalk-message/rest-api)
- [카카오 기본 템플릿 제한](https://developers.kakao.com/docs/latest/en/message-template/default)
- [카카오 로그인 REST API](https://developers.kakao.com/docs/latest/en/kakaologin/rest-api)
- [Azure Communication Services Email](https://learn.microsoft.com/en-us/azure/communication-services/concepts/email/email-overview)
- [Copilot 훅](https://docs.github.com/en/copilot/reference/hooks-reference)
- [VS Code 훅](https://code.visualstudio.com/docs/agents/reference/hooks-reference)
