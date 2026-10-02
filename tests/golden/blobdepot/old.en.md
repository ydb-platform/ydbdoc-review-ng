# Blob Depot

<!-- golden presentation reference derived from EN blobdepot.md at base of #50839
     base: 1705aa4cea8caaf8c715b368f57ac7317975af83
     backticks added on identifier atoms for presentation-map coverage
-->

### How to start {#vg-create-params}

A virtual group is created through `BS_CONTROLLER` by passing a special command. In case of repeated command execution, an error will be returned with the `Already: true` field filled.

```bash
dstool -e ... --direct group virtual create --name vg1 vg2 --hive-id=72057594037968897 --storage-pool-name=/Root:virtual --log-channel-sp=/Root:ssd --data-channel-sp=/Root:ssd*8
```

Command-line parameters:

* `--name` — unique name for the virtual group (or several virtual groups with similar parameters)
* `--hive-id=N` — number of the Hive tablet that will manage this blob depot
* `--storage-pool-name=` / pool name `POOL_NAME` within which the blob depot needs to be created
* `--storage-pool-id=BOX:POOL` — alternative to `--storage-pool-name`
* `--log-channel-sp=` pool `POOL_NAME` for channel 0
* `--wait` — wait for blob depot creation to complete

### How to check that everything has started {#vg-check-running}

* via the `BS_CONTROLLER` monitoring page
* via the `dstool group list --virtual-groups-only` command

Control creation through the `VirtualGroupName` field, which should match what was passed in the `--name` parameter. The `VirtualGroupState` field can take one of the following values:

* `NEW` — group is waiting for initialization
* `WORKING` — group is created and working
* `CREATE_FAILED` — an error occurred during group creation

### `BS_CONTROLLER` Monitoring Page {#diag-bscontroller}

On the `BS_CONTROLLER` monitoring page there is a special Virtual groups tab.

State | Blob depot state; can be NEW, WORKING, CREATED_FAILED.

ErrorReason | For `CREATE_FAILED` state contains a text description of the creation error reason.
