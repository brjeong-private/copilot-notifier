# Copilot Notifier

GitHub Copilot의 **Autopilot 작업 완료를 카카오톡 나와의 채팅으로 알려주는**
Python 기반 Windows 도구입니다. 별도 서버나 추가 AI 요약 모델은 사용하지 않습니다.
Azure Communication Services(ACS) 이메일은 배포·수동 테스트만 지원합니다.

## 준비

Windows, Python 3.10 이상, Copilot CLI가 필요합니다. Python 외부 패키지는 없으며,
Windows Copilot 훅 실행에는 PowerShell 7 이상이 필요합니다. 기능 로직은 모두
`scripts\notifier.py`에 있습니다.

아래 명령은 저장소 루트에서 실행합니다. `uv`를 쓰는 경우 `python` 대신
`uv run --python 3.12 python`을 사용하면 됩니다.

## 카카오톡 연결

1. [Kakao Developers](https://developers.kakao.com/)에서 앱을 만들고 카카오 로그인을 활성화합니다.
2. **카카오 로그인 → 동의항목**에서 **카카오톡 메시지 전송(`talk_message`)**을 설정합니다.
3. **앱 → 플랫폼 키 → 사용하는 REST API 키**에서 Redirect URI를
   `http://localhost:53682/`로 등록합니다. 마지막 `/`까지 일치해야 합니다.
4. REST API 키와 Client Secret을 준비하고, 메시지 버튼에 사용할 웹 도메인
   (예: `https://github.com`)을 앱에 등록합니다.

```console
python .\scripts\notifier.py connect-kakao --message-url "https://github.com/YOUR-USERNAME/YOUR-REPOSITORY"
python .\scripts\notifier.py send-test-kakao
python .\scripts\notifier.py install-hook
```

연결 명령의 숨김 입력창에 키와 Secret을 입력합니다. 브라우저에서 로그인할 때
**메시지 전송에 체크하고 동의**한 뒤, 터미널의 `Kakao credentials saved encrypted...`
문구까지 확인하세요. 로그인 대기는 3분 후 만료됩니다.

테스트 메시지를 확인하고 훅을 설치한 뒤 Copilot CLI를 재시작합니다.
VS Code는 Copilot harness가 같은 SDK 이벤트 기록을 제공할 때만 지원하며,
설치 후 창을 다시 로드해야 합니다. Local harness는 지원하지 않습니다.

### 알림 내용과 범위

프로젝트명, 시작·완료 시각, 소요 시간, 요청·결과 요약과 프로젝트 링크를 보냅니다.
텍스트는 전체 200자 이내이며, 기존 요청·완료 요약을 잘라 사용합니다.

- 명시적인 Autopilot 모드의 수락된 루트 완료 이벤트만 발송합니다.
- 일반 챗·Plan·하위 에이전트 완료·입력 대기·실패에는 알리지 않습니다.
- 이메일 자동 발송, 채널 동시 발송, `.env` 로딩은 아직 지원하지 않습니다.
  [`.env.example`](.env.example)은 향후 설정 예시일 뿐 현재 동작을 바꾸지 않습니다.

## Azure 이메일 — 수동 테스트

Azure CLI와 배포·발송 권한이 필요합니다. 아래 배포 명령은 **실제 리소스를 생성**하므로
구독·수신 주소와 비용을 확인하세요. 카카오톡만 사용한다면 필요하지 않습니다.

```console
az login
python .\scripts\notifier.py deploy-email --subscription-id "YOUR-SUBSCRIPTION-ID" --recipient "you@example.com" --resource-group "YOUR-RESOURCE-GROUP" --location "westus3"
python .\scripts\notifier.py send-test-email
```

`infra\email.json`으로 Azure 관리 이메일 도메인과 연결된 ACS 리소스를 배포합니다.
데이터 위치는 `United States`이며, 발송 인증은 Azure CLI의 Microsoft Entra 토큰을
사용합니다. 액세스 키는 저장하지 않습니다. 테스트 성공은 ACS 발송 작업 성공이며,
받은편지함 도착 확인은 아닙니다.

## 설정·주의사항

| 위치 | 용도 |
|---|---|
| `.local\kakao.json` | 현재 Windows 사용자용 DPAPI로 암호화한 카카오 인증정보 |
| `.local\config.json` | Azure 배포 결과와 이메일 설정 |
| `.local\kakao-receipts` / `.local\receipts` | 메시지 본문·인증정보를 제외한 발송 상태 |
| `%USERPROFILE%\.copilot\hooks\tools-copilot-notifier.json` | 사용자 완료 훅. `COPILOT_HOME` 설정 시 해당 폴더 아래 `hooks` 사용 |

카카오 토큰은 자동 갱신하지만 동의 철회·만료 시 다시 연결해야 합니다.
이전 `.local\kakao.xml`은 자동 이전되지 않습니다.
훅은 설치 당시 Python과 스크립트 경로를 참조하므로 이동 시 다시 설치하세요.
비활성화하려면 위 훅 JSON 파일만 삭제합니다.

완료 건별 중복 발송을 방지합니다. 카카오 전송 결과가 불확실하면 `Pending`을 남기고
자동 재시도하지 않습니다. 채팅과 실패 원인을 확인하기 전에는 발송 기록을 삭제하지 마세요.
API 성공은 읽음이나 휴대폰 푸시를 보장하지 않습니다.

`.env`와 `.local`은 Git에서 제외됩니다. 키·토큰·실제 수신 주소는 커밋하거나 공유하지 마세요.
코드 블록과 자격 증명처럼 보이는 줄을 걸러도 완전한 유출 방지는 아니므로,
**짧은 요청·결과 요약을 카카오/Azure로 전송해도 되는 작업에만 사용하세요.**

## 문제 해결

| 증상 | 확인할 내용 |
|---|---|
| `KOE205` | 앱에서 `talk_message` 동의항목이 설정되어 있는지 확인 |
| 등록하지 않은 Redirect URI | 사용하는 REST API 키에 `http://localhost:53682/` 등록 |
| `403 / -402` | 연결 명령을 다시 실행하고 브라우저에서 메시지 전송에 동의 |
| `Login received...`만 표시 | 브라우저가 아닌 원래 터미널에서 토큰 저장 성공 여부 확인 |

오프라인 테스트:

```console
python .\scripts\notifier.py test
```
