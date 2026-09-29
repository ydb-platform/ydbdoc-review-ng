# Архитектурные инварианты

## Источники данных

- Source читается из immutable snapshot исходного PR или зафиксированного base
  tip по уже реализованным контрактам T003–T006.
- Candidate собирается из source plan, валидных model values и exact protected
  fragments исходника. Старый target не является материалом для склейки.
- URL, path, anchor и код являются protected fragments, модель не задаёт их
  содержимое. Внутренний YDB URL меняет только locale, Wikipedia может получить
  официальный target URL через fail-open `langlinks`, остальные значения берутся
  из source. Parse и полный Diplodoc build проверяют результат до публикации.
- Отсутствующая симметричная внутренняя статья становится dependency scope,
  переводится целиком и получает зеркальную TOC-запись. Иной существующий target
  по соответствующей старой ссылке только создаёт `YELLOW` о вероятном
  дубликате; его bytes не участвуют в сборке новой статьи.

## Минимальный поток

`doc_translate` идёт линейно: create job audit → authorize → snapshot →
budget → direction/scope → parse → translate → local structural validation and
safe Markdown normalization → critic-editor → independent arbiter → full validation/Diplodoc build → one commit/push
→ PR verdict → terminal job status.

`doc_verify` создаёт job audit, берёт текущую translation branch и authoritative
source, запускает те же validators и critic-editor, при необходимости применяет
его валидные изменённые исправления одним commit в ту же branch, затем обновляет
verdict и terminal job status. Budget
gate у него отсутствует.

`doc_continue` в `1.1.0` создаёт audit, проверяет label actor и последний допустимый
предшествующий `/ydbdoc continue` comment, затем загружает живой checkpoint.
Для translation PR checkpoint выбирается по точным provenance `source_sha` и
текущему `target_sha`; старые открытые checkpoints того же source PR не мешают.
После такого admission checkpoint потребляется guarded update по точному ID и
состоянию, без повторного глобального поиска по source PR. Поэтому исторические
независимые цепочки не превращают уже выбранную запись в ambiguous.
Единственный acknowledged `pending` successor может быть идемпотентно открыт
при точном provenance translation PR; это восстанавливает потерянное
acknowledgement, но не разрешает recovery через широкий source-PR lookup.
Для source PR неоднозначность остаётся fail-closed.
Replay читает только сохранённые source/base SHA и проверяет scope/field IDs
и exact translation head. Три stage: direction retry, перевод pending документов
с accepted maps и critic-editor только для unresolved review paths. Source-only
assembly и обычные проверки сохраняются. GREEN закрывает checkpoint; повторный
semantic stop наследует первоначальный expiry. Infrastructure failure не является
новым semantic checkpoint. Общей resumable state machine нет.

## Разделение ответственности

- Локальный детерминированный слой проверяет strict JSON, exact field IDs,
  placeholders/container pairs, source-fragment restoration, повторный parse и
  простые build-breaking дефекты Markdown. Потерянные моделью обязательные
  пустые строки вокруг заголовков и списков восстанавливаются во всём candidate,
  если такого дефекта не было в source. Невалидный большой translate chunk
  после одной correction рекурсивно делится по top-level block boundary, пока
  диапазон source blocks строго уменьшается; неделимый невалидный chunk не
  публикуется.
- Publication validator накладывает candidate на trusted base checkout и до
  каждого commit запускает полный официальный Diplodoc build. Любой `ERR` или
  ненулевой exit запрещает публикацию; нефатальный `WARN` не блокирует её.
  Privileged workflow не checkout-ит и не исполняет
  содержимое source PR.
- В `doc_verify` protected-fragment invariant отвергает ручное изменение
  URL/path/code относительно вычисленного ожидаемого значения. Глобальный
  navigation graph не строится; узкий resolver знает только YDB locale и
  Wikipedia `langlinks`, результат проверяет полный Diplodoc build.
- Model critic сравнивает authoritative source и final target, проверяя смысл,
  полноту, терминологию и работоспособность ссылок. Если совместный prompt велик,
  critic получает соответствующие source/target excerpt-пары, а не полный target
  рядом с каждым source excerpt; verdict и findings объединяются.
- Add source-TOC-reachable страницы добавляет target TOC entry без redirect;
  add вне source TOC не обязан менять TOC; rename обновляет target TOC path и
  создаёт прямой redirect old→new; ordinary edit не меняет ни TOC, ни redirects.
- YDB хранит job, versioned continuation checkpoint и каждую начатую model attempt с request, response при наличии,
  status/error и cost. Job всегда завершается terminal status/error, включая
  ранние failures. TTL текстов составляет 14 дней.
- Единственный QA comment является коротким русским пользовательским резюме:
  номер исходного PR, цветной вердикт, стоимость текущей job, по одному
  конкретному исправлению на файл максимум для десяти файлов и краткая
  инструкция `doc_continue` при семантическом RED. В исходном PR один
  marker-комментарий содержит ссылку на translation PR и обновляется без
  создания дублей. SHA, накопительные
  breakdown и внутренние диагностические данные остаются в YDB и публично не
  выводятся.
- Budget gate выполняется только перед новым `doc_translate`. Его дневной `SUM`
  включает все известные costs трёх workflow и ролей, в том числе
  `doc_verify` critic-editor; unknown cost не подменяется нулём. Gate идёт после
  authorization/snapshot и до любого model call, включая mixed-locale
  direction call.

## Совместимость

`v1.0.x` фиксирует входы `doc_translate` и `doc_verify`; `1.1.0` добавляет
`continue --pr N` без source/target/budget override. Все режимы возвращают exit 1
при RED. Action проверяет входы по mode до Python. Consumer label template
находится в `docs/examples/doc_continue.yml`; repo-local dispatch workflows
не подписаны на labels чужого репозитория. Реальное подключение consumer и
публикация release tag выполняются отдельно после финальной приёмки.

## Зависимые ресурсы и чтение snapshot

`runtime_assets.missing_assets` использует parser-owned link/image destinations,
добавляет отсутствующие симметричные изображения/PDF в fixed_files и сохраняет
существующую target-локализацию. Новые bytes берутся только из source snapshot;
verify и continuation повторно вычисляют этот набор от pinned base. Модель не
обрабатывает бинарные ресурсы. Лимит: 100 файлов и 20 MiB на candidate.

GitHubBackend кэширует только успешное чтение bytes/отсутствия по immutable
snapshot/path (4096 записей, 16 MiB). Ошибки и текущие heads не кэшируются.
GitHubHTTP ограниченно повторяет только идемпотентные GET при transport error,
429 и 5xx (две короткие задержки); мутации не повторяются.

## Copied prose guard (2026-09-28)

The published candidate in run 36388057226 contained untranslated Russian prose
in five English documents. Parser inspection confirmed that these were normal
model-visible fields, not protected blocks. RU→EN chunk acceptance now rejects
verbatim Russian spans of at least 32 letters, ignoring whitespace differences.
Protected code/URLs/templates are placeholders before this check. The existing
single translator correction and recursive split handle this validation error.
Short names/terms and non-verbatim semantic errors still require the critic.

## Decimal prose versus paths (2026-09-28)

The path recognizer previously treated decimal 1.79 as a filename. Russian
1,79 has no protected path, so normal localization failed protected-fragment
validation. Bare decimals (including signed/exponent forms) are excluded from
PATH; explicit/nested numeric paths, filenames with an extension, URLs and
inline code remain protected. This does not weaken numeric semantic review.

## Complete build overlay on reruns (2026-09-28)

DiplodocBuildValidator must overlay every plan entry, including before == after.
Those bytes describe the remote translation head, not the local base checkout.
Filtering by remote diff omitted unchanged TOC/assets on reruns and skipped all
validation for unchanged candidates. Build overlays and restores the full plan;
Git publication still uses real diffs for commit/no-op decisions.

## Translation baseline preflight (2026-09-28)

Production RuntimeContent receives DiplodocBuildValidator.validate_baseline when
YDBDOC_DOCS_ROOT is configured. doc_translate invokes it before prepare_source,
including before model-assisted direction selection. It builds the untouched
trusted checkout with the same compiler/error policy as candidate validation.
A broken base fails prepare with trusted_base_build trace, before model calls
or publication; it is not a recoverable translation checkpoint. Candidate build
still runs after translation and before every publication. Verify/continue retain
their existing candidate validation and do not add this translation preflight.

## Translation branch replacement on reruns (2026-09-28)

A fresh `doc_translate` candidate is based on the pinned translation base, never
on an older `translation/pr-N` head. Snapshot resolution records the old branch
head separately. Publication may force-replace that ref only when a fresh read
still equals the recorded SHA; otherwise it fails closed. Critic edits happen
before this single push. Existing PR provenance and `Checked translation commit` are rewritten
to the exact newly published SHA. This prevents reruns from producing a branch
that is both behind and ahead of the current base while preserving concurrent
manual edits.

## Critic-editor followed by an independent arbiter (2026-09-29)

Semantic defects such as duplicated glossary aliases are intentionally not
encoded as growing local heuristics. The translator creates a structurally valid
draft. For every document or aligned excerpt the critic-editor returns only
minimal findings (`reason`, `searchable_snippet`, `expected_correction`) and the
complete corrected Markdown. Editor findings are audit diagnostics and do not
define the public verdict. The editor never receives them back for another
semantic call. A separate read-only arbiter, using the translator model by
default, evaluates the corrected excerpt and alone returns GREEN/RED findings.
There is no model ping-pong and no model-produced `repairable`, path, line,
verdict or field IDs in the editor response. One schema-invalid
HTTP-success response may be repeated once with the parser reason; this is a
bounded technical retry, not a semantic review loop. A provider
`non_final`/truncated response is likewise retried at most once with the
identical request; a second unfinished generation fails closed. The edits are
assembled, validated, built and published once. A full draft build must not
run before the critic-editor: fixable draft lint errors would otherwise prevent
the editor from running. Mechanical list-marker spacing is normalized locally
outside code before review; the final candidate always receives the strict build.
The critic has its own 48000-character request budget. Its schema omits field
IDs because a complete corrected Markdown excerpt is the edit unit; this keeps
large documents to a few large calls instead of dozens of top-level-block calls.
During `doc_translate`, these excerpts are the
exact validated translator chunk pairs retained in memory. The review stage
does not parse and heuristically realign the assembled source and target again.
If the critic provider exhausts its bounded content-filter retry, one fallback
critic pass reuses each exact validated translator chunk pair contained in that
excerpt through a flat `corrected_markdown`-only schema. The translated target
is never split proportionally to source block lengths and fallback units are
never split recursively.

## Public readiness report (2026-09-29)

A readiness `YELLOW` is not an arbiter finding. The public report explicitly
states that semantic review found no blocking translation defects and that
required CI has not yet confirmed merge readiness. It does not expose a raw
snapshot such as `check not started`. The actionable next step is to wait for
`build-docs` and apply the `doc_verify` label. `doc_continue` is offered only
for a semantic RED that has a durable continuation checkpoint; it cannot be
truthfully offered for a readiness-only YELLOW.

When `doc_verify` publishes an editor correction, that push creates a new head
after the previous `build-docs` result. The reporter therefore polls boundedly
for `build-docs` on the exact published SHA before writing its final comment.
This wait performs no model calls. A failed check produces RED; a timeout keeps
the explicit readiness YELLOW.
