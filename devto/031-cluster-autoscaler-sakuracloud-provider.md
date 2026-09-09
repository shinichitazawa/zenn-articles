---
title: Writing a Cluster Autoscaler cloud provider for SAKURA Cloud
published: false
description: SAKURA Cloud has no Auto Scaling Group equivalent, so the Cluster Autoscaler provider has to create and delete servers itself. Notes from building one and sending it upstream.
tags: kubernetes, go, cloud, opensource
canonical_url: https://zenn.dev/shinichitazawa/articles/031-cluster-autoscaler-sakuracloud-provider
---

> This is an English version of [an article I first published in Japanese](https://zenn.dev/shinichitazawa/articles/031-cluster-autoscaler-sakuracloud-provider). [SAKURA Cloud](https://manual.sakura.ad.jp/cloud-api/1.1/) is an IaaS run by a Japanese provider; its API is public and documented, but there is no official Cluster Autoscaler support for it.

I run a small hybrid Kubernetes cluster: a Raspberry Pi at home acts as the k3s control plane, and VMs from several clouds join it over a Tailscale overlay. [Cluster Autoscaler](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/FAQ.md) handles AWS, GCP and Azure nodes there without static credentials. SAKURA Cloud was the one provider left out, so I wrote a cloud provider for it and sent it upstream.

The interesting part is not the Go code — it is that **SAKURA Cloud has no group primitive to scale**, which changes the shape of the provider, and that a handful of API behaviours only showed up once I ran it against the real thing.

- Who this is for: people who have touched Cluster Autoscaler, Go, and an IaaS API
- Measurements and timings are from my own environment (August 2026); yours may differ. API keys, startup scripts and account-specific IDs are redacted as `<...>`

{% details Note on AI assistance %}
I used AI (Anthropic Claude) while writing and editing this article. Technical claims are verified against the official documentation quoted inline. Corrections are welcome in the comments.
{% enddetails %}

## Why write one: there is no ASG equivalent

The `CloudProvider` interface in Cluster Autoscaler is built around a **NodeGroup** that can be resized. The AWS provider maps a NodeGroup to an Auto Scaling Group, GCP to a Managed Instance Group, Azure to a Virtual Machine Scale Set. All of them assume the cloud offers an API that takes a desired count and does the rest.

```text
AWS / GCP / Azure provider
  Cluster Autoscaler --(set desired size)--> ASG / MIG / VMSS --> cloud creates or deletes the servers

SAKURA Cloud provider (this article)
  Cluster Autoscaler --(create and delete one at a time, itself)--> server + disk
                                                                    (no group abstraction exists)
```

The [SAKURA Cloud API v1.1](https://manual.sakura.ad.jp/cloud-api/1.1/) has no such group abstraction: the units of operation are individual servers and disks. The existing implementation model for that situation is the [Hetzner Cloud provider](https://github.com/kubernetes/autoscaler/tree/master/cluster-autoscaler/cloudprovider/hetzner), where the provider itself creates and deletes one server per unit of scale. I followed the same approach and added `cluster-autoscaler/cloudprovider/sakuracloud/` to my fork. There was no existing implementation upstream, and I could not find a public one elsewhere.

## Shape of the implementation

The provider is three files:

| File | Role |
|---|---|
| `sakuracloud_cloud_provider.go` | The `CloudProvider` implementation (listing NodeGroups, `NodeGroupForNode`, …) |
| `sakuracloud_manager.go` | The layer that calls the SAKURA API directly (server/disk CRUD, plan resolution) |
| `sakuracloud_node_group.go` | The `NodeGroup` implementation (`IncreaseSize` / `DeleteNodes` / `TargetSize`) |

There is no external SDK dependency; it calls REST directly with `net/http`. The API base is per zone, `https://secure.sakura.ad.jp/cloud/zone/<zone>/api/cloud/1.1`, and authentication is HTTP Basic with an API access token and secret.

Two conventions let Cluster Autoscaler identify nodes and groups:

- **providerID**: `sakuracloud://<zone>/<serverName>`. This is the value behind kubelet's `--provider-id`; the server name makes it unique.
- **Group membership tag**: `ca-group-<nodeGroupName>`. Created servers carry this tag, and listing servers resolves them back to a group.

One thing to be aware of if you are writing a provider today: the master branch of Cluster Autoscaler changed how providers register. A provider now self-registers by calling `builder.RegisterCloudProvider` from `init()`, and `cloudprovider/router/` blank-imports it behind a build tag. See the [`init()` in the hetzner provider](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/cloudprovider/hetzner/hetzner_cloud_provider.go) and [router/router_hetzner.go](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/cloudprovider/router/router_hetzner.go). The shared packages, including `builder`, now live under `sigs.k8s.io/cluster-autoscaler/pkg/*`, which the [go.mod on master](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/go.mod) references. (All as of September 2026.) The PR follows this newer scheme.

## The API sequence for adding a node

This is the call order behind a single `IncreaseSize`. The ordering and the waits are the whole story — every surprise I hit is in here.

```go
// 1. Create the disk, then wait until it becomes available
//    (POST /disk. Right after creation it is unusable until Status=available)
diskID := createDisk(...)
waitDiskAvailable(diskID)

// 2. Create the server. ServerPlan takes CPU/MemoryMB, not a plan ID
doRequest("POST", "/server", map[string]any{
    "Server": map[string]any{
        "Name":              name,
        "ServerPlan":        map[string]any{"CPU": core, "MemoryMB": memGB * 1024},
        "Tags":              []string{"ca-group-" + group},
        "ConnectedSwitches": []map[string]any{{"Scope": "shared"}},
    },
})

// 3. Attach the disk, then inject hostname and the startup script
doRequest("PUT", "/disk/"+diskID+"/to/server/"+serverID, nil)
doRequest("PUT", "/disk/"+diskID+"/config", map[string]any{
    "HostName": name,
    "Notes":    []map[string]any{{"ID": "<startup-note-id>"}},
})

// 4. Writing the config puts the disk back into a non-available state,
//    so wait for available again (powering on too early returns 409 disk_is_not_available)
waitDiskAvailable(diskID)

// 5. Power on (PUT /server/:id/power)
doRequest("PUT", "/server/"+serverID+"/power", nil)
```

The official documentation for [`POST /server`](https://manual.sakura.ad.jp/cloud-api/1.1/server/index.html) shows `ServerPlan` given as resource values such as `{"CPU": 2, "MemoryMB": 4096, ...}` rather than a plan identifier. Disks are created with [`POST /disk`](https://manual.sakura.ad.jp/cloud-api/1.1/disk/index.html), and the documentation states that a freshly created disk cannot be used until its status becomes available, so the code waits before the next operation. Hostname and startup script injection go through [`PUT /disk/:diskid/config`](https://manual.sakura.ad.jp/cloud-api/1.1/disk/index.html).

## Five behaviours I only learned by running it

Implementing straight from the documentation was not enough. These five showed up in practice.

| # | Observed behaviour | What I did about it |
|---|---|---|
| 1 | Passing a plan by ID when creating a server returns 400 | Specify `ServerPlan` as CPU/MemoryMB resource values (which is also the form the documentation shows) |
| 2 | Right after `PUT /disk/:id/config` the disk goes back to "changing", and powering on immediately returns 409 `disk_is_not_available` | Wait for available a second time after the config write, then power on |
| 3 | The server list response does not include power state | Always force-stop before deletion, and ignore the 409 `power_must_be_down` returned when it is already stopped |
| 4 | There is no OIDC federation with an external identity provider, so the keyless setup used for AWS/GCP/Azure is not possible | Pass a static API key (token/secret) through a Secret |
| 5 | A failure part-way through can leave orphaned servers and disks | Make them findable: the `ca-group-<nodeGroupName>` tag plus `<nodeGroupName>-<random>` server naming |

Behaviour 2 is the flip side of two statements in the documentation — that a disk attached to a running server cannot be rewritten, and that a newly created disk is unusable until available. Writing the config also puts the disk into a temporarily non-available state; that part I measured rather than read. For behaviour 3, the force stop matches the documented server power-off call [`DELETE /server/:id/power`](https://manual.sakura.ad.jp/cloud-api/1.1/server/index.html) accepting `Force: true`. Deletion itself passes the disk ID as `WithDisk` to [`DELETE /server/:id`](https://manual.sakura.ad.jp/cloud-api/1.1/server/index.html) so the server and its disk go together.

Behaviour 4 is the significant difference from the other three clouds. With AWS, GCP and Azure I can run the autoscaler keyless, using a self-hosted OIDC issuer. For SAKURA Cloud a static API key is required: the [official API key documentation](https://manual.sakura.ad.jp/cloud/api/apikey.html) describes an access token and access token secret as the authentication method, with no equivalent of OIDC federation with an external IdP documented (as of September 2026).

## Verifying the whole chain: KEDA to Cluster Autoscaler to 0 → 1 → 0

I ran the provider on the real cluster and confirmed the full scaling chain (August 2026).

1. [KEDA](https://keda.sh/) (Kubernetes Event-driven Autoscaling) scales a workload up, producing a pending pod with a `nodeSelector`.
2. Cluster Autoscaler decides the matching NodeGroup goes 0 → 1 and creates a server through the flow above. Creation through power-on took about 7 minutes in my measurements.
3. On boot, the startup script joins Tailscale and then joins k3s. The node appears in the cluster, the pending pod lands on it and goes Running.
4. When the scaling condition clears, Cluster Autoscaler deletes the server with [`DELETE /server/:id`](https://manual.sakura.ad.jp/cloud-api/1.1/server/index.html) (`WithDisk`), and the SAKURA side returns to zero servers.

The whole cycle completed without intervention, and the SAKURA control panel showed the server count going 0 → 1 → 0.

```text
KEDA fires        -> pending pod with nodeSelector appears
CA decides 0 -> 1  -> create disk -> wait available -> create server -> config -> wait available -> power on
(~7 min measured) -> startup script joins Tailscale + k3s
node appears      -> pending pod is scheduled onto it and goes Running
condition clears  -> CA calls DELETE /server/:id (WithDisk) -> back to zero servers
```

## Sending it upstream

| PR | Contents |
|---|---|
| [#10146](https://github.com/kubernetes/autoscaler/pull/10146) | Add the sakuracloud provider (provider, tests, OWNERS, README, FAQ) |
| [#10220](https://github.com/kubernetes/autoscaler/pull/10220) | Fix the GCE provider erroring out on foreign providerIDs, which surfaces when nodes from other clouds are mixed into one cluster |

Adding a new provider upstream comes with an expectation: development and ongoing maintenance of that provider belong to the submitter as the cloudprovider owner, and the core maintainers generally do not get involved in provider-specific code ([cloudprovider/POLICY.md](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/cloudprovider/POLICY.md), retrieved September 2026). Getting agreement from SIG Autoscaling during review is another difference from just running a fork internally.

## Takeaways

- With no ASG/MIG equivalent, the provider has to be the Hetzner shape: it creates and deletes servers and disks itself.
- Node creation is create disk → wait available → create server (plan as CPU/MemoryMB) → attach and configure the disk → **wait available again** → power on. Skipping the second wait fails with [`disk_is_not_available`](https://manual.sakura.ad.jp/cloud-api/1.1/disk/index.html).
- Five API behaviours only appeared when running against the real API: plan-by-ID 400, the second availability wait, no power state in the list response, no OIDC federation, and orphaned resources after a partial failure.
- The KEDA → Cluster Autoscaler → SAKURA 0 → 1 → 0 chain works on real hardware, and the implementation is up as a PR.

## References

- [SAKURA Cloud API v1.1](https://manual.sakura.ad.jp/cloud-api/1.1/) — [servers](https://manual.sakura.ad.jp/cloud-api/1.1/server/index.html) / [disks](https://manual.sakura.ad.jp/cloud-api/1.1/disk/index.html) / [products and plans](https://manual.sakura.ad.jp/cloud-api/1.1/product/index.html) (Japanese)
- [Cluster Autoscaler FAQ](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/FAQ.md) / [cloudprovider POLICY.md](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/cloudprovider/POLICY.md)
- [Hetzner cloudprovider](https://github.com/kubernetes/autoscaler/tree/master/cluster-autoscaler/cloudprovider/hetzner) — the implementation model
- [PR #10146: add SAKURA cloud (sakuracloud) cloud provider](https://github.com/kubernetes/autoscaler/pull/10146)
- Provider implementation (fork): [the `sakuracloud-provider` branch](https://github.com/shinichitazawa/autoscaler/tree/sakuracloud-provider/cluster-autoscaler/cloudprovider/sakuracloud) — the head of PR #10146, including a README and configuration example
