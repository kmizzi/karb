# Karb Infrastructure

Infrastructure as code for deploying the Karb arbitrage bot.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    AWS us-east-1                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │              Bot Server (t4g.small)                  │    │
│  │  • Scanner: WebSocket to Polymarket                  │    │
│  │  • Executor: Places orders via proxy                 │    │
│  │  • Dashboard: karb.arkets.com:8080                   │    │
│  └───────────────────────┬─────────────────────────────┘    │
└──────────────────────────│──────────────────────────────────┘
                           │ SOCKS5 (port 1080)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                AWS ca-central-1 (Montreal)                   │
│  ┌─────────────────────────────────────────────────────┐    │
│  │              Proxy Server (t4g.nano)                 │    │
│  │  • SOCKS5 proxy (dante)                              │    │
│  │  • Routes order API calls to Polymarket              │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

## Prerequisites

- [OpenTofu](https://opentofu.org/) (or Terraform)
- [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/intro_installation.html)
- AWS CLI configured with credentials
- Cloudflare API token (for DNS)

## Quick Start

### 1. Configure OpenTofu

```bash
cd opentofu

# Copy and edit variables
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your values

# Initialize and apply
tofu init
tofu plan
tofu apply
```

### 2. Configure Ansible

```bash
cd ../ansible

# Copy and edit variables
cp group_vars/all.yml.example group_vars/all.yml
# Edit group_vars/all.yml with your secrets

# Update inventory with IPs from tofu output
# Or set environment variables:
export KARB_BOT_IP=$(cd ../opentofu && tofu output -raw bot_public_ip)
export KARB_PROXY_IP=$(cd ../opentofu && tofu output -raw proxy_public_ip)
```

### 3. Deploy

```bash
# Deploy everything
ansible-playbook playbooks/site.yml

# Or deploy individually
ansible-playbook playbooks/proxy.yml
ansible-playbook playbooks/bot.yml
```

### 4. Verify

```bash
# Check bot status
ssh ubuntu@$KARB_BOT_IP "sudo systemctl status karb-bot"

# Check dashboard
curl -u admin:password https://karb.arkets.com:8080/api/status

# Check proxy
ssh ubuntu@$KARB_PROXY_IP "sudo systemctl status danted"
```

## Estimated Costs

| Resource | Type | Monthly Cost |
|----------|------|--------------|
| Bot server | t4g.small | ~$12 |
| Proxy server | t4g.nano | ~$3 |
| **Total** | | **~$15/month** |

## Useful Commands

```bash
# View bot logs
ssh ubuntu@$KARB_BOT_IP "sudo journalctl -u karb-bot -f"

# Restart bot
ssh ubuntu@$KARB_BOT_IP "sudo systemctl restart karb-bot"

# Update code and redeploy
ansible-playbook playbooks/bot.yml

# Destroy infrastructure
cd opentofu && tofu destroy
```

## Security Notes

- SSH access is restricted to IPs in `ssh_allowed_cidrs`
- Proxy only accepts connections from the bot server
- Dashboard requires HTTP Basic Auth
- All secrets are in `group_vars/all.yml` (not committed to git)
