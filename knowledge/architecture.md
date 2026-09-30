# Архитектурные инварианты

Полный PR review ниже утверждён 2026-09-30, но не реализован и не проверен
реальными providers на baseline `2a1c268`. Факты существующей реализации
отделены от новых обязательных границ; исторические excerpt probes не являются
приёмкой этого контракта.

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
safe Markdown normalization → full PR editor → runtime validation/apply → full PR arbiter → semantic verdict.
Build/CI не участвуют в семантическом вердикте. RED findings идут непосредственно
в отчёт, автоматически не исправляются и никуда не передаются.

`doc_verify` создаёт job audit, берёт текущую translation branch и authoritative
source и запускает полный PR editor → runtime validation/apply → полный PR arbiter. Валидные изменения публикуются одним exact-head non-force
commit в ту же branch, затем обновляются verdict и terminal job status.
Budget gate у него отсутствует.

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
с accepted maps и полный PR critic/runtime/arbiter при любом semantic review.
Review paths служат диагностикой. Новый review contract version связывает
source/candidate/glossary manifests и digests; старые chunk-review checkpoints
отвергаются fail closed. Source-only
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
- PR editor и arbiter получают весь closed PR context: полный inventory,
  review-owned source/candidate text files, полный pinned RU/EN glossary,
  разрешённые paths/operations, snapshot/content digests и technical validation
  data. Source glossary берётся из authoritative snapshot, target glossary из
  pinned translation base. Старый target prose не служит материалом repair.
  Missing files/glossary, oversize или unknown capability останавливают review.
- Add source-TOC-reachable страницы добавляет target TOC entry без redirect;
  add вне source TOC не обязан менять TOC; rename обновляет target TOC path и
  создаёт прямой redirect old→new. Ordinary edit также проверяет симметричную
  TOC-запись как пару `name + href`: корректная запись остаётся byte-identical,
  отсутствующая добавляется, а устаревший `name` исправляется по H1 существующей
  target-статьи. Redirect при этом не создаётся.
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

## Historical translation baseline preflight (2026-09-28)

This records the old implementation, not the active semantic process. Build/CI
do not participate in the semantic verdict.

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

## Full PR editor, runtime and independent arbiter (approved 2026-09-30)

One editor call returns a strict `{"files": {path: complete_text}}` map of exactly
all editable target text paths, including unchanged files. Runtime rejects
missing, unknown, duplicate, malformed, partial, binary, deletion, traversal or
out-of-scope entries. It checks source-owned technical values and metadata/plan constraints and
applies the entire map atomically in memory. A no-op edit still receives the
independent arbiter review.

One independent read-only arbiter reviews the complete final candidate as a complete
PR with both complete glossaries. GREEN requires a valid full final response,
empty findings, complete nonempty coverage and matching candidate/context digest.
Changed editor bytes, empty responses or a missing arbiter never imply GREEN.
The prompts actively check semantic completeness, cross-file terminology/entity
identity even without exact glossary mappings, commands/parameters/identifiers,
technical-literal boundaries/readability, links/anchors, H1/index/TOC consistency
and every planned PR operation. RED findings go directly to the report and are never automatically repaired
or passed onward. Build/CI do not participate in the semantic verdict.
No bytes may change after arbiter without a new full review.

Translator chunking and retries remain translator-only. Editor/arbiter context
is indivisible: no excerpt packing, filtered glossary or lossy fallback. Admission
reserves verified model input/context/output capacity for the exact serialized
request and complete-file output, with JSON overhead. Unknown capability or
oversize is a typed terminal failure before publication. The paired witness
glossaries already contain 225432 UTF-8 bytes; the historical 48000-character
excerpt limit and old provider probes are insufficient. Review retry policy is
the single bounded policy in REQUIREMENTS_RU.md §5: at most two primary attempts
per role, optional one full-context editor fallback after exhausted content
filter and fresh capacity admission.

PR review attempts use nullable `target_path` with explicit PR scope, contract
version and context digest. They retain cost/audit without arbitrary article
attribution or classification as historical unattributed cost. Real-provider
full-context probe and independent content review are required before claiming
semantic acceptance; scripted offline GREEN proves orchestration only.

## Public semantic report (2026-09-30)

The QA comment reports translation quality only. Its GREEN/RED is the arbiter
verdict; a translation-specific probable-duplicate warning may produce YELLOW.
Repository CI, `build-docs`, `doc_verify` check-runs and merge readiness are
separate GitHub signals. The reporter neither reads nor waits for them, and
their state cannot downgrade an arbiter GREEN or upgrade an arbiter RED.

`doc_continue` is offered only with a semantic RED and its durable checkpoint.
GREEN contains no CI instructions and no unusable continuation recipe.
# Explicit translation-plan boundary (2026-09-30)

The source PR file inventory is the workflow input boundary. A pure
classification preflight runs before any model call. After direction selection,
`translation_plan.py` assigns every row an explicit disposition before metadata
production, classifies both sides of renames, validates status/action pairs and
claims exact target outputs. Fixed-output and final-candidate reconciliation
then block publication when a planned TOC/delete/rename/document result is
missing.

Supported TOC migrations bind the SHA-256 of the complete expected target file
into the plan before document-model calls. Fixed and final reconciliation both
require the exact digest, so path presence cannot mask stale content. This is
the existing implementation boundary. Under the approved PR review contract,
only explicitly planned TOC name corrections may update the validated artifact
digest through a planner-owned API retaining original plan and correction
provenance. Structure, href, ordering and valid target-only entries remain
protected; arbitrary YAML overwrite or disabling reconciliation is forbidden.
A correction outside this scope fails closed. General TOC translation remains
separate executor work.

This is a narrow fail-closed planner, not the completed cross-file executor.
Unsupported asset/redirect operations, metadata-only direction and TOC
remove/rename/copy stop before publication. The target architecture and policy
matrix are in `docs/translation-plan.md`: model translation uses the complete
file; source base→head deltas are programmatic planning inputs only; every plan
entry gets a mutation or checked-noop proof; the canonical plan/result hashes
are replayed by verify/continue. An article H1 is context, not a general
translation of a navigation label.
