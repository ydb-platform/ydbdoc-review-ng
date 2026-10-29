# Conducting Load Testing

{{ ydb-short-name }} has a built-in toolkit for performing load tests using standard benchmarks:

| Benchmark                            | Reference                                                |
|--------------------------------------|----------------------------------------------------------|
| [TPC-C](https://tpc.org/tpcc/)       | [tpcc](../../reference/ydb-cli/workload-tpcc.md) |
| [TPC-H](https://tpc.org/tpch/)       | [tpch](../../reference/ydb-cli/workload-tpch.md)|
| [TPC-DS](https://tpc.org/tpcds/)     | [tpcds](../../reference/ydb-cli/workload-tpcds.md)|
| [ClickBench](https://benchmark.clickhouse.com/) | [clickbench](../../reference/ydb-cli/workload-click-bench.md)|

In addition to standard benchmarks, there are several internal ones:

| Benchmark                            | Reference                                                |
|--------------------------------------|----------------------------------------------------------|
| `Key Value` | [kv](../../reference/ydb-cli/workload-kv.md)|
| `Stock` | [stock](../../reference/ydb-cli/commands/workload/stock.md)|
| `Topic` | [topic](../../reference/ydb-cli/workload-topic.md)|
| `Transfer` | [topic](../../reference/ydb-cli/workload-transfer.md)|

There is also a possibility to run user-defined testing scenarios, which are initiated via the `ydb workload query` command, see the [description](../../reference/ydb-cli/workload-query.md). The details are provided in the corresponding section.

All mentioned methods emulate the user's workload on the database under the given scenarios. The detailed description of each method is provided in the corresponding sections, the links to which are provided above.

All commands for working with benchmarks are organized into corresponding groups:

```bash
{{ ydb-cli }} workload tpcc --path path/in/database ...
{{ ydb-cli }} workload clickbench --path path/in/database ...
{{ ydb-cli }} workload tpch --path path/in/database ...
{{ ydb-cli }} workload tpcds --path path/in/database ...
{{ ydb-cli }} workload query --path path/in/database ...
{{ ydb-cli }} workload kv --path path/in/database ...
{{ ydb-cli }} workload stock --path path/in/database ...
{{ ydb-cli }} workload topic ...
{{ ydb-cli }} workload transfer ...
```

Load testing consists of three stages:

1. [Data preparation](#data-preparation)
1. [Testing](#testing)
1. [Cleanup](#cleanup)

## Data Preparation {#data-preparation}

It consists of two steps: initializing tables and filling them with data.

### Initialization

Initialization is performed by the `init` command:

```bash
{{ ydb-cli }} workload tpcc --path tpcc/10wh init
{{ ydb-cli }} workload clickbench --path clickbench/hits init --store=row
{{ ydb-cli }} workload tpch --path tpch/s1 init --store=column
{{ ydb-cli }} workload tpcds --path tpcds/s1 init --store=external-s3
{{ ydb-cli }} workload query --path user/suite1 init --suite-path /home/user/user_suite
{{ ydb-cli }} workload kv --path kv init --store=column
{{ ydb-cli }} workload stock --path stock init --store=row
{{ ydb-cli }} workload topic init --topic some_topic
{{ ydb-cli }} workload transfer topic-to-table init --topic some_topic --table /db/table
```

At the stage of creating tables, if you are running `tpch`, `tpcds`, or `clickbench`, you can additionally configure the tables to be created:

* Select the type of tables to be used: row, column, external, etc. (parameter `--store`);
* Select the types of columns to be used: string types (`--string`), date and time types (`--datetime`), and floating-point number types (`--float-mode`).

You can also specify that tables should be deleted before creation if they already exist (parameter `--clear`).

For more details, see the command descriptions for each benchmark:

* [tpcc init](../../reference/ydb-cli/workload-tpcc.md#init)
* [clickbench init](../../reference/ydb-cli/workload-click-bench.md#init)
* [tpch init](../../reference/ydb-cli/workload-tpch.md#init)
* [tpcds init](../../reference/ydb-cli/workload-tpcds.md#init)
* [query init](../../reference/ydb-cli/workload-query.md#init)
* [kv init](../../reference/ydb-cli/workload-kv.md#init)
* [stock init](../../reference/ydb-cli/commands/workload/stock.md#init)
* [topic init](../../reference/ydb-cli/workload-topic.md#init)
* [transfer init](../../reference/ydb-cli/workload-transfer.md#init)

### Loading Data into the Tables

The `import` command is used to load data into the created tables. This command is specific to each benchmark, and its behavior depends on additional options.

For more details, see the relevant reference sections:

* [tpcc import](../../reference/ydb-cli/workload-tpcc.md#load)
* [clickbench import](../../reference/ydb-cli/workload-click-bench.md#load)
* [tpch import](../../reference/ydb-cli/workload-tpch.md#load)
* [tpcds import](../../reference/ydb-cli/workload-tpcds.md#load)
* [query import](../../reference/ydb-cli/workload-query.md#load)

Examples:

```bash
{{ ydb-cli }} workload tpcc --path tpcc/10wh import
{{ ydb-cli }} workload clickbench --path clickbench/hits import files --input hits.csv.gz
{{ ydb-cli }} workload tpch --path tpch/s1 import generator --scale 1
{{ ydb-cli }} workload tpcds --path tpcds/s1 import generator --scale 1
{{ ydb-cli }} workload query --path user/suite1 import --suite-path /home/user/user_suite
```

## Testing {#testing}

The load testing itself is performed using the `run` command. Its behavior is mostly the same across different benchmarks, though some differences do exist.

Examples:

```bash
{{ ydb-cli }} workload tpcc --path tpcc/10wh run
{{ ydb-cli }} workload clickbench --path clickbench/hits run --include 1-5,8
{{ ydb-cli }} workload tpch --path tpch/s1 run --exсlude 3,4 --iterations 3
{{ ydb-cli }} workload tpcds --path tpcds/s1 run --plan ~/query_plan --include 2 --iterations 5
{{ ydb-cli }} workload query --path user/suite1 run --plan ~/query_plan --include first_query_set.1.sql,second_query_set.2.sql --iterations 5
{{ ydb-cli }} workload kv --path kv run mixed
{{ ydb-cli }} workload stock --path stock run add-rand-order
{{ ydb-cli }} workload topic run full --topic some_topic
{{ ydb-cli }} workload transfer topic-to-table run --topic some_topic --table /db/table
```

The `run` command for each benchmark has a number of additional parameters for configuring the types of reports generated, collecting statistics, and other results of load testing.

For more details, see the relevant reference sections:

* [tpcc run](../../reference/ydb-cli/workload-tpcc.md#run)
* [clickbench run](../../reference/ydb-cli/workload-click-bench.md#run)
* [tpch run](../../reference/ydb-cli/workload-tpch.md#run)
* [tpcds run](../../reference/ydb-cli/workload-tpcds.md#run)
* [query run](../../reference/ydb-cli/workload-query.md#run)
* [kv run](../../reference/ydb-cli/workload-kv.md#run)
* [stock run](../../reference/ydb-cli/commands/workload/stock.md#run)
* [topic run](../../reference/ydb-cli/workload-topic.md#run)
* [transfer run](../../reference/ydb-cli/workload-transfer.md#run)

## Cleanup {#cleanup}

Upon completion of load testing, the test data and tables can be removed using the `clean` command:

```bash
{{ ydb-cli }} workload tpcc --path tpcc/10wh clean
{{ ydb-cli }} workload clickbench --path clickbench/hits clean
{{ ydb-cli }} workload tpch --path tpch/s1 clean
{{ ydb-cli }} workload tpcds --path tpcds/s1 clean
{{ ydb-cli }} workload query --path user/suite1 clean
{{ ydb-cli }} workload kv --path kv clean
{{ ydb-cli }} workload stock --path stock clean
{{ ydb-cli }} workload topic clean --topic some_topic
{{ ydb-cli }} workload transfer topic-to-table clean --topic some_topic --table /db/table
```

For more details, see the relevant reference sections:

* [tpcc clean](../../reference/ydb-cli/workload-tpcc.md#cleanup)
* [clickbench clean](../../reference/ydb-cli/workload-click-bench.md#cleanup)
* [tpch clean](../../reference/ydb-cli/workload-tpch.md#cleanup)
* [tpcds clean](../../reference/ydb-cli/workload-tpcds.md#cleanup)
* [query clean](../../reference/ydb-cli/workload-query.md#cleanup)
* [kv clean](../../reference/ydb-cli/workload-kv.md#clean)
* stock clean
* [topic clean](../../reference/ydb-cli/workload-topic.md#clean)
* [transfer clean](../../reference/ydb-cli/workload-transfer.md#clean)