#!/bin/bash
# tms.sh — curl wrapper for the TMS REST API
# Requires bash 4+ and curl 7.55+ (for `-H @file`).

BASE="https://tms.inf.elte.hu/api"
TOKEN_FILE="${TMS_TOKEN_FILE:-$HOME/.tms_token}"

# ---------- login ----------
_login() {
  local USER PASS resp token body eu ep
  if [ -n "${TMS_USER:-}" ] && [ -n "${TMS_PASS:-}" ]; then
    USER="$TMS_USER"; PASS="$TMS_PASS"
  elif [ -t 0 ]; then
    read -rp  "TMS username: " USER
    read -rsp "TMS password: " PASS; printf '\n' >&2
  else
    echo "tms: no TMS_USER/TMS_PASS set and no tty — cannot prompt" >&2
    return 1
  fi
  [ -n "$USER" ] && [ -n "$PASS" ] || { echo "tms: empty credentials" >&2; return 1; }

  if command -v jq >/dev/null 2>&1; then
    body=$(jq -cn --arg u "$USER" --arg p "$PASS" '{username:$u,password:$p}')
  else
    eu=${USER//\\/\\\\}; eu=${eu//\"/\\\"}
    ep=${PASS//\\/\\\\}; ep=${ep//\"/\\\"}
    body=$(printf '{"username":"%s","password":"%s"}' "$eu" "$ep")
  fi
  unset PASS

  resp=$(printf '%s' "$body" | curl -sS --connect-timeout 10 --max-time 30 \
    -X POST "$BASE/common/auth/ldap-login" \
    -H 'Content-Type: application/json' --data-binary @-) || {
    echo "tms: login request failed (network/TLS)" >&2; return 1; }

  if command -v jq >/dev/null 2>&1; then
    token=$(printf '%s' "$resp" | jq -r '.accessToken // empty' 2>/dev/null)
  fi
  [ -n "${token:-}" ] || token=$(printf '%s' "$resp" \
    | grep -o '"accessToken":"[^"]*"' | head -1 | cut -d'"' -f4)
  [ -n "$token" ] || {
    echo "login failed — server did not return a token:" >&2
    printf '%s\n' "$resp" >&2
    return 1; }

  rm -f "$TOKEN_FILE"
  ( umask 077; printf '%s' "$token" > "$TOKEN_FILE" )
  echo "token cached: $TOKEN_FILE" >&2
}

# ---------- help ----------
_help_all() { cat <<'EOF'
tms — curl wrapper for the TMS REST API

USAGE
  tms [-X METHOD] [-d JSON] [-F field=@file]... [-H 'Header: value']... [-o OUT] [-i] [-v] <path>
  tms -h [base]         short endpoint list (common|student|instructor|admin)
  tms -m [base]         extended manual — field schemas, examples
  tms --token-status    cached token location and validity

BASES
  common      auth, semesters, user settings, notifications, user lookup
  student     groups, tasks, submissions, quizzes, attendance
  instructor  courses, groups, quizzes (role required)
  admin       semesters, ip-restriction, statistics (role required)

EXAMPLES
  tms /common/user-settings
  tms /common/semesters
  tms '/student/groups?semesterID=<SEM>'
  tms '/student/tasks?groupID=<GID>&expand=submission'
  tms -o sub.zip /student/submissions/<SUBID>/download
  tms -X POST -F taskID=<TID> -F file=@solution.hs /student/submissions/upload

TOKEN
  Cached at $TMS_TOKEN_FILE (default ~/.tms_token), mode 600.
  Auto re-login on 401 (once per call).
  Headless login: TMS_USER=<neptun> TMS_PASS=<pw> tms ...
  Wipe:           rm -f ~/.tms_token

NOTES
  Quote any URL containing ? or &.
  API baseURL is /api — paths below omit that prefix.
  Output is raw JSON; pipe to `jq` for pretty-printing.
EOF
}

_help_for() { case "$1" in
  common) cat <<'EOF'
/common — auth, semesters, user
  GET    /common/user-settings
  PUT    /common/user-settings
  POST   /common/user-settings/confirm-email?code=<CODE>
  GET    /common/semesters
  GET    /common/notifications
  POST   /common/notifications/dismiss?notificationID=<ID>
  GET    /common/system/public-info
  GET    /common/system/private-info
  GET    /common/users/student?text=<QUERY>
  GET    /common/users/faculty?text=<QUERY>
  POST   /common/auth/ldap-login       {username,password} → {accessToken,imageToken}
  POST   /common/auth/mock-login
  POST   /common/auth/logout
EOF
  ;;
  student) cat <<'EOF'
/student — read, download, submit
  GET    /student/groups?semesterID=<SEM>
  GET    /student/groups/<GID>?expand=course,notes
  GET    /student/groups/<GID>/attendances
  GET    /student/groups/<GID>/submissions/<SUBID>/jwt
  GET    /student/tasks?groupID=<GID>&expand=submission
  GET    /student/tasks/<TID>?expand=submission,taskFiles,submission.codeCheckerResult,submission.codeCheckerResult.codeCheckerReports
  POST   /student/tasks/<TID>/unlock
  GET    /student/task-files/<FID>/download              BINARY  → use -o
  GET    /student/submissions
  GET    /student/submissions/<SUBID>/download           BINARY  → use -o
  GET    /student/submissions/<SUBID>/download-report    BINARY  → use -o
  GET    /student/submissions/<SUBID>/auto-tester-results
  POST   /student/submissions/upload                     MULTIPART  → -F taskID= -F file=@
  POST   /student/submissions/verify
  GET    /student/quiz-test-instances?semesterID=<SEM>
  GET    /student/quiz-test-instances/<QID>
  GET    /student/quiz-test-instances/<QID>/duration
  GET    /student/quiz-test-instances/<QID>/results
  POST   /student/quiz-test-instances/<QID>/start-write
  POST   /student/quiz-test-instances/<QID>/finish-write
  POST   /student/quiz-test-instances/unlock
  GET    /student/attendance?semesterID=<SEM>
  POST   /student/canvas/sync-submission?taskID=<TID>
EOF
  ;;
  instructor) cat <<'EOF'
/instructor — teaching-side (role required)
  CRUD   /instructor/courses [/<ID>]        .../lecturers[/<UID>]
  CRUD   /instructor/groups  [/<ID>]        .../students[/<UID>][/notes]
                                            .../tasks[/<TID>]
                                            .../attendance-periods[/.../attendances]
                                            .../instructors[/<UID>]
                                            .../stats
  CRUD   /instructor/tasks   [/<ID>]        ?expand=groups,taskLevelGitRepo,ipRestrictions
  CRUD   /instructor/submissions
  CRUD   /instructor/quiz-tests             .../duplicate  .../finalize  .../start
  CRUD   /instructor/quiz-questions         /list-for-set /list-for-test
  CRUD   /instructor/quiz-question-sets     .../duplicate  .../images
  CRUD   /instructor/quiz-answers
  CRUD   /instructor/quiz-test-instances    /ip-addresses
  CRUD   /instructor/quiz-test-personal-settings
  CRUD   /instructor/attendance(-periods)
  CRUD   /instructor/notifications
  CRUD   /instructor/plagiarism(-basefile)  .../<ID>/run  /services
  GET    /instructor/canvas/{courses,sections}
  POST   /instructor/canvas/{setup,sync,cancel-sync}?groupID=<GID>
  GET    /instructor/groups/<GID>/export-{tasks,quizzes}-spreadsheet
EOF
  ;;
  admin) cat <<'EOF'
/admin — administrator (role required)
  CRUD   /admin/courses
  CRUD   /admin/semesters
  CRUD   /admin/notifications
  CRUD   /admin/ip-restriction
  CRUD   /admin/evaluator-templates     .../additional-information
  GET    /admin/statistics [/view]
EOF
  ;;
  *) echo "unknown base: $1 (use common|student|instructor|admin)" >&2; return 1 ;;
esac
}

# ---------- manual ----------
_man() {
  case "${1:-}" in
    ""|all|common|student|instructor|admin) ;;
    *) echo "unknown base: $1" >&2; return 1 ;;
  esac
  local s="${1:-all}"

  if [ "$s" = all ] || [ "$s" = common ]; then cat <<'EOF'
============================================================
 COMMON
============================================================
GET /common/user-settings
  Response (placeholders):
    {
      "name": "<full name>",         "userCode": "<neptun>",
      "email": "<neptun>@inf.elte.hu",
      "customEmail": null,           "locale": "en-US",
      "isStudent": true,             "isFaculty": false,
      "isAdmin": false,              "isLecturer": false,
      "defaultRole": "default",      "customEmailConfirmed": 0,
      "notificationTarget": "official"
    }

PUT /common/user-settings
  Body: any subset of fields above.
  Example (change one field):
    tms -X PUT -d '{"notificationTarget":"official"}' /common/user-settings

GET /common/semesters
  Response:
    [ { "id": <SEM>, "name": "YYYY/YY/N",
        "startDate": "YYYY-MM-DD", "endDate": "YYYY-MM-DD",
        "actual": true|false }, ... ]
  Tip — find the active semester:
    tms /common/semesters | jq -r '.[] | select(.actual) | .id'

GET /common/notifications                 → list (may be [])
POST /common/notifications/dismiss?notificationID=<ID>
GET /common/system/public-info
GET /common/system/private-info
GET /common/users/student?text=<QUERY>
GET /common/users/faculty?text=<QUERY>

POST /common/auth/ldap-login
  Body: {"username":"<neptun>","password":"<password>"}
  Resp: {"accessToken":"<token>","imageToken":"<token>"}
  (handled automatically by tms — you normally never call this)
EOF
  fi

  if [ "$s" = all ] || [ "$s" = student ]; then cat <<'EOF'

============================================================
 STUDENT
============================================================
GET /student/groups?semesterID=<SEM>              (required: semesterID)
  Response: list of groups:
    {
      "id": <GID>, "number": <int>,
      "course": { "id": <CID>, "name": "<course name>",
                  "codes": ["<code>"], "lecturerNames": ["<name>"] },
      "instructorNames": ["<name>", ...], "timezone": "Europe/Budapest",
      "isCanvasCourse": false, "canvasUrl": null, "lastSyncTime": null,
      "isExamGroup": false, "groupType": "regular",
      "day": null, "startTime": null, "roomNumber": ""
    }

GET /student/groups/<GID>?expand=course,notes
GET /student/groups/<GID>/attendances

GET /student/tasks?groupID=<GID>&expand=submission   (required: groupID)
  Response: list-of-lists (grouped by category). Each task:
    { "id": <TID>, "name": "<task name>",
      "category": "<cat>", "translatedCategory": "<cat>",
      "description": "<markdown>",
      "softDeadline": null | "<iso>", "hardDeadline": "<iso>",
      "available": "<iso>", "creatorName": "<name>", "semesterID": <SEM>,
      "autoTest": 0|1, "staticCodeAnalysis": 0|1,
      "entryPasswordProtected": false, "submissionLimit": 0,
      "isSubmissionCountRestricted": false, "appType": "Console",
      "submission": {
        "id": <SUBID>, "name": "<filename>", "uploadTime": "<iso>",
        "status": "Accepted|Rejected|...", "translatedStatus": "<status>",
        "grade": <num>, "notes": "<grader note>", "graderName": "<name>",
        "lastGradedAt": "<iso>", "errorMsg": "<msg>"|null,
        "taskID": <TID>, "verified": true|false
      } }

GET /student/tasks/<TID>?expand=submission,taskFiles,submission.codeCheckerResult,submission.codeCheckerResult.codeCheckerReports
  One task with everything (adds taskFiles[] and submission.codeCheckerResult).

POST /student/tasks/<TID>/unlock

GET /student/task-files/<FID>/download              BINARY — use -o
  tms -o file.bin /student/task-files/<FID>/download

GET /student/submissions
GET /student/submissions/<SUBID>/download           BINARY — use -o
  tms -o sub.zip /student/submissions/<SUBID>/download && unzip -l sub.zip
GET /student/submissions/<SUBID>/download-report    BINARY — use -o
GET /student/submissions/<SUBID>/auto-tester-results

POST /student/submissions/upload                    MULTIPART
  Fields the SPA sends:
    taskID = <TID>       (string)
    file   = @<path>     (blob)
  tms -X POST -F taskID=<TID> -F file=@solution.hs /student/submissions/upload
  Response: a submission object (same shape as `submission` above).

GET  /student/quiz-test-instances?semesterID=<SEM>
GET  /student/quiz-test-instances/<QID>
GET  /student/quiz-test-instances/<QID>/duration
GET  /student/quiz-test-instances/<QID>/results
POST /student/quiz-test-instances/<QID>/start-write
POST /student/quiz-test-instances/<QID>/finish-write
POST /student/quiz-test-instances/unlock

GET  /student/attendance?semesterID=<SEM>
POST /student/canvas/sync-submission?taskID=<TID>
EOF
  fi

  if [ "$s" = all ] || [ "$s" = instructor ]; then cat <<'EOF'

============================================================
 INSTRUCTOR (role required)
============================================================
  CRUD /instructor/courses      [/<ID>]  .../lecturers[/<UID>]
  CRUD /instructor/groups       [/<ID>]  .../students[/<UID>][/notes]
                                         .../tasks[/<TID>]
                                         .../attendance-periods[/.../attendances]
                                         .../instructors[/<UID>]  .../stats
  CRUD /instructor/tasks        [/<ID>]  ?expand=groups,taskLevelGitRepo,ipRestrictions
  CRUD /instructor/submissions

  CRUD /instructor/quiz-tests           .../duplicate  .../finalize  .../start
  CRUD /instructor/quiz-questions       /list-for-set /list-for-test
  CRUD /instructor/quiz-question-sets   .../duplicate  .../images
  CRUD /instructor/quiz-answers
  CRUD /instructor/quiz-test-instances  /ip-addresses
  CRUD /instructor/quiz-test-personal-settings

  CRUD /instructor/attendance(-periods)
  CRUD /instructor/notifications
  CRUD /instructor/plagiarism(-basefile)  .../<ID>/run  /services

  GET  /instructor/canvas/{courses,sections}
  POST /instructor/canvas/setup?groupID=<GID>
  POST /instructor/canvas/sync?groupID=<GID>
  POST /instructor/canvas/cancel-sync?groupID=<GID>
  GET  /instructor/groups/<GID>/export-tasks-spreadsheet
  GET  /instructor/groups/<GID>/export-quizzes-spreadsheet
EOF
  fi

  if [ "$s" = all ] || [ "$s" = admin ]; then cat <<'EOF'

============================================================
 ADMIN (role required)
============================================================
  CRUD /admin/courses
  CRUD /admin/semesters
  CRUD /admin/notifications
  CRUD /admin/ip-restriction
  CRUD /admin/evaluator-templates     .../additional-information
  GET  /admin/statistics [/view]
EOF
  fi
}

# ---------- flag dispatch ----------
case "$1" in
  -h|--help)
    shift
    if [ -z "$1" ]; then _help_all; else _help_for "$1" || exit 1; fi
    exit 0 ;;
  -m|--man)
    shift
    _man "${1:-}" || exit 1
    exit 0 ;;
  --token-status)
    if [ ! -s "$TOKEN_FILE" ]; then
      echo "no cached token at $TOKEN_FILE"
      exit 0
    fi
    command -v curl >/dev/null || { echo "tms: curl not found" >&2; exit 127; }
    ls -l "$TOKEN_FILE" 2>/dev/null
    _af=$(mktemp); chmod 600 "$_af"
    printf 'Authorization: Bearer %s\n' "$(<"$TOKEN_FILE")" > "$_af"
    _code=$(curl -sS --connect-timeout 10 --max-time 30 \
      -o /dev/null -w '%{http_code}' -H "@$_af" \
      "$BASE/common/user-settings") || _code="000"
    rm -f "$_af"
    case "$_code" in
      200) echo "token: VALID" ;;
      401) echo "token: EXPIRED (will re-login on next call)" ;;
      *)   echo "token: UNKNOWN (HTTP $_code)" ;;
    esac
    exit 0 ;;
  "" )
    echo "tms: no arguments — try  tms -h  or  tms -m" >&2
    exit 1 ;;
esac

# ---------- arg parse ----------
method="GET"; data=""; out=""; include=0; verbose=0
forms=(); headers=()
while [ $# -gt 0 ]; do
  case "$1" in
    -X) method="$2";   shift 2 ;;
    -d) data="$2";     shift 2 ;;
    -F) forms+=("$2"); shift 2 ;;
    -H) headers+=("$2"); shift 2 ;;
    -o) out="$2";      shift 2 ;;
    -i) include=1;     shift ;;
    -v) verbose=1;     shift ;;
    --) shift; break ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *)  break ;;
  esac
done
[ -n "$1" ] || {
  echo "usage: tms [-X METHOD] [-d JSON] [-F f=@file]... [-H 'K: V']... [-o OUT] [-i] [-v] <path>" >&2
  exit 1; }
path="$1"
case "$path" in /*) ;; *) path="/$path" ;; esac

command -v curl >/dev/null || { echo "tms: curl not found in PATH" >&2; exit 127; }

# ---------- token ensure ----------
if [ ! -s "$TOKEN_FILE" ]; then
  _login || exit 1
else
  p=$(stat -c '%a' "$TOKEN_FILE" 2>/dev/null \
       || stat -f '%Lp' "$TOKEN_FILE" 2>/dev/null || echo "")
  if [ -n "$p" ] && [ "$p" != "600" ]; then
    chmod 600 "$TOKEN_FILE" 2>/dev/null \
      && echo "note: tightened $TOKEN_FILE to 600" >&2
  fi
fi

# ---------- build curl args (auth added per-attempt from temp file) ----------
cargs=(-sS -X "$method" --connect-timeout 10 --max-time 120)
[ "$include" = 1 ] && cargs+=(-i)
[ -n "$data" ] && cargs+=(-H 'Content-Type: application/json' --data-binary "$data")
for h in "${headers[@]}"; do cargs+=(-H "$h"); done
for f in "${forms[@]}";   do cargs+=(-F "$f"); done
[ "$verbose" = 1 ] && cargs+=(-v)

# ---------- request with one re-auth ----------
authf=$(mktemp); chmod 600 "$authf"

for attempt in 1 2; do
  printf 'Authorization: Bearer %s\n' "$(<"$TOKEN_FILE")" > "$authf"

  if [ -n "$out" ]; then
    code=$(curl "${cargs[@]}" -H "@$authf" -o "$out" -w '%{http_code}' "$BASE$path") || {
      rm -f "$authf"; echo "tms: curl failed (network/TLS)" >&2; exit 1; }
  else
    tmpf=$(mktemp)
    code=$(curl "${cargs[@]}" -H "@$authf" -o "$tmpf" -w '%{http_code}' "$BASE$path") || {
      rm -f "$tmpf" "$authf"; echo "tms: curl failed (network/TLS)" >&2; exit 1; }
    body=$(cat "$tmpf"); rm -f "$tmpf"
  fi

  if [ "$code" = "401" ] && [ "$attempt" = "1" ]; then
    echo "token expired (401) — re-authenticating..." >&2
    rm -f "$TOKEN_FILE"
    _login || { rm -f "$authf"; exit 1; }
    continue
  fi

  rm -f "$authf"

  if [ -n "$out" ]; then
    echo "$code -> $out" >&2
    [ "$code" -ge 400 ] && exit 1
    exit 0
  fi

  if [ "$code" -ge 400 ]; then
    printf 'HTTP %s:\n' "$code" >&2
    printf '%s\n' "$body" >&2
    exit 1
  fi
  printf '%s\n' "$body"
  exit 0
done
exit 1
