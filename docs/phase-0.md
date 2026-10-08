# Phase 0: first alert

Goal: open a shell in the API pod and get a Discord ping from Falco.

Every command runs inside your Ubuntu VM, from the root of this folder unless it says otherwise.

## 0. Connect over SSH and copy these files in

Work from Terminal on your Mac over SSH, not in the VMware window. The VMware console can't paste, and most of this guide is pasting.

```bash
ssh bsenatus@<vm-ip>          # the VM console shows its IP at login, or run `ip -br addr` there
```

If that says "Connection refused", the VM has no SSH server yet. Type this once in the VMware console, then try again:

```bash
sudo apt install -y openssh-server
```

Fix the VM's clock before anything else. A clock that is off by weeks breaks apt and every TLS download:

```bash
sudo timedatectl set-ntp true
date                          # should show today's date
```

Copy this folder into the VM. In a second Terminal tab on your Mac (not the SSH one):

```bash
scp ~/Downloads/blast-radius-phase0.tar.gz bsenatus@<vm-ip>:~
```

Then, back in the SSH session:

```bash
tar xzf blast-radius-phase0.tar.gz && cd blast-radius
```

## The pieces, in terms you already know

| Thing | What it is | Closest thing you know |
|---|---|---|
| Docker image | A packaged filesystem plus the command to run | A VM image, but only one process and no kernel |
| k3s | A full Kubernetes in one small binary | AKS, minus the cloud |
| Pod / Deployment | A running container / the spec that keeps N pods alive | A VM / a scale set |
| Service | A stable in-cluster address in front of pods | An internal load balancer |
| Helm chart | A templated bundle of Kubernetes YAML | A Terraform module |
| `values.yaml` | The inputs to that chart | `terraform.tfvars` |
| Falco | Watches every syscall on the node and matches rules | A SIEM detection rule, at the kernel |
| Falcosidekick | Takes Falco alerts and forwards them (Discord, Elastic, ...) | A Logstash output |

## 1. Make room

```bash
df -h /
```

Aim for about 6 GB free. If you are short:

```bash
sudo apt clean
sudo journalctl --vacuum-size=100M
docker system prune -af   # only if Docker is already installed and you don't need its old images
```

The VM should also have at least 4 GB of RAM for k3s plus Falco.

## 2. Install k3s

```bash
curl -sfL https://get.k3s.io | sh -s - --disable traefik
```

`--disable traefik` skips the built-in ingress controller. You don't need it yet, and it saves disk and memory.

Give your own user a kubeconfig (readable only by you, which is the secure default):

```bash
mkdir -p ~/.kube
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown "$USER": ~/.kube/config && chmod 600 ~/.kube/config
echo 'export KUBECONFIG=~/.kube/config' >> ~/.bashrc && source ~/.bashrc

kubectl get nodes        # STATUS should be Ready
```

## 3. Install Docker and Helm

```bash
sudo apt update && sudo apt install -y docker.io
sudo usermod -aG docker "$USER"
newgrp docker            # or log out and back in
docker run --rm hello-world

sudo snap install helm --classic
helm version
```

Docker and k3s each have their own container store. You build with Docker, then copy the image into k3s (step 5).

## 4. Make the Discord webhook

In Discord: create a channel `#blast-radius`, then Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL.

Load it into your shell without it landing in your bash history, and test it. When the `Webhook URL:` prompt appears, paste the URL and press Enter. Nothing shows on screen while you paste; that is on purpose, like a password prompt.

```bash
read -rsp "Webhook URL: " DISCORD_WEBHOOK_URL && export DISCORD_WEBHOOK_URL && echo
curl -s -H "Content-Type: application/json" \
  -d '{"content":"blast-radius webhook test"}' "$DISCORD_WEBHOOK_URL"
```

You should see the test message in the channel. Treat this URL like a password: anyone with it can post to your server. It never goes in a file in this repo.

## 5. Build the API image and copy it into k3s

```bash
docker build -t blast-radius-api:dev .
```

Optional local check before Kubernetes gets involved:

```bash
echo "fake-local-1234" > /tmp/api-key
docker run --rm -d --name api-test -p 8000:8000 \
  -v /tmp/api-key:/etc/blast-radius/api-key:ro blast-radius-api:dev
curl localhost:8000/healthz
curl localhost:8000/config        # {"api_key":"****1234"}
docker rm -f api-test
```

Import into k3s. The `:dev` tag plus `imagePullPolicy: Never` in the Deployment means k3s uses this local copy and never tries Docker Hub:

```bash
docker save blast-radius-api:dev | sudo k3s ctr images import -
sudo k3s crictl images | grep blast-radius
```

## 6. Deploy the API

```bash
kubectl apply -f k8s/namespace.yaml

# The fake secret the app reads. Created from the command line so it never sits in git.
kubectl -n app create secret generic api-key \
  --from-literal=api-key="fake-$(openssl rand -hex 12)"

kubectl apply -f k8s/
kubectl -n app get pods -w        # wait for 1/1 Running, then Ctrl-C
```

Check it works through the Service:

```bash
kubectl -n app port-forward svc/api 8080:80 &
curl localhost:8080/items
curl localhost:8080/config
kill %1
```

The Deployment already runs as a non-root user with a read-only filesystem, no Linux capabilities and a seccomp profile. That is a head start on Phase 1: it should pass Pod Security Admission at `restricted` without changes.

## 7. Install Falco and Falcosidekick

```bash
helm repo add falcosecurity https://falcosecurity.github.io/charts
helm repo update

helm install falco falcosecurity/falco --version 9.2.0 \
  -n security --create-namespace \
  -f falco/values.yaml \
  --set-string falcosidekick.config.discord.webhookurl="$DISCORD_WEBHOOK_URL"

kubectl -n security get pods -w
```

Wait until the `falco-...` pod shows `2/2 Running` and `falco-falcosidekick-...` shows `1/1 Running`. The first start pulls a few images, so give it a minute or two.

What `falco/values.yaml` sets, and why:

- `driver.kind: modern_ebpf`: hooks the kernel with eBPF using the kernel's own type info (BTF). No kernel headers, no compiled module, which matters on ARM64 and on a small disk.
- `collectors.containerEngine...containerd.sockets`: k3s runs its own containerd at `/run/k3s/containerd/containerd.sock`. Falco needs it to turn a container ID into a pod name and namespace.
- `falcosidekick.enabled: true`: deploys Sidekick and points Falco's HTTP output at it.
- `minimumpriority: notice`: the shell-in-container rule fires at Notice, so anything lower would be dropped.

Check the driver loaded:

```bash
kubectl -n security logs ds/falco -c falco | head -40
```

Look for a line mentioning the modern BPF probe being opened, and no errors after it.

## 8. Trigger it

```bash
kubectl -n app exec -it deploy/api -- /bin/sh
```

Type `exit`. Within a few seconds `#blast-radius` should show a Notice alert like "A shell was spawned in a container with an attached terminal", including `k8s.ns.name=app` and the pod name. Screenshot it.

The `-it` matters: the rule only fires when a terminal is attached.

Confirm the path end to end if you want to see it in logs too:

```bash
kubectl -n security logs ds/falco -c falco | grep -i "shell"
kubectl -n security logs deploy/falco-falcosidekick | grep -i discord   # expect "Discord - POST OK"
```

## 9. Commit it

Create an empty GitHub repo named `blast-radius`, then:

```bash
git init -b main
git add .
git commit -m "Phase 0: FastAPI on k3s with Falco alerts to Discord"
git remote add origin git@github.com:BrendanSenatus/blast-radius.git
git push -u origin main
```

Before pushing, `git grep -n discord.com` should return nothing.

## Seeing what's running

The VM has no desktop, and it doesn't need one.

**Discord** is the dashboard for Phase 0. An alert there means every piece worked, from Falco's kernel hook to the webhook.

**k9s** is a live dashboard that runs inside the terminal, so it works over SSH:

```bash
curl -sL https://github.com/derailed/k9s/releases/download/v0.51.0/k9s_Linux_arm64.tar.gz | tar xz k9s
sudo install k9s /usr/local/bin/ && rm k9s
k9s
```

Press `0` to show every namespace, arrow to a pod, then `l` for its logs, `d` to describe it, or `s` for a shell (which also fires the Falco alert). `Esc` goes back and `:q` quits.

**Your Mac's browser** can open the API's interactive docs page through an SSH tunnel. On your Mac:

```bash
ssh -L 8080:localhost:8080 bsenatus@<vm-ip>
```

In that session run `kubectl -n app port-forward svc/api 8080:80`, then open http://localhost:8080/docs on your Mac. The port-forward listens only on the VM's localhost, so nothing else on the network can reach the API.

Phase 3 adds a proper dashboard: Falco events in Kibana.

## If something goes wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| Pod stuck at `ErrImageNeverPull` | Image not imported into k3s, or the tag differs | Re-run the `docker save ... \| sudo k3s ctr images import -` line, check `sudo k3s crictl images` |
| Pod `CrashLoopBackOff` | App error | `kubectl -n app logs deploy/api` |
| Falco pod `CrashLoopBackOff`, logs mention BPF or BTF | Kernel lacks BTF | Check `uname -r` and `ls /sys/kernel/btf/vmlinux`. Fallback: `helm upgrade falco falcosecurity/falco -n security --reuse-values --set driver.kind=kmod` (needs kernel headers, more disk) |
| Alert in Falco logs but nothing in Discord | Webhook URL wrong or not passed | Sidekick logs; then `helm upgrade falco falcosecurity/falco -n security --reuse-values --set-string falcosidekick.config.discord.webhookurl="$DISCORD_WEBHOOK_URL"` |
| Alert shows `k8s.pod.name=<NA>` | Falco can't reach k3s's containerd | `ls -l /run/k3s/containerd/containerd.sock`, check the socket path in `falco/values.yaml` |
| No alert at all | Exec without a terminal | Use `kubectl exec -it`, not just `kubectl exec` |
| Pods `Evicted`, node `DiskPressure` | Disk nearly full | `df -h /`, then `docker system prune -af` (images already in k3s are safe) |

## Tear down or start over

```bash
helm uninstall falco -n security
kubectl delete namespace app security
/usr/local/bin/k3s-uninstall.sh     # removes k3s entirely
```
