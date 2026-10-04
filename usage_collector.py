#!/usr/bin/env python3
"""
Usage Dashboard Collector — gathers free-tier resource usage from
Oracle Cloud / Cloudflare / Google Drive / GitHub and writes usage.json.

Run periodically (every 3h) via cron. Output: usage.json (ready for GitHub Pages).
Each source is guarded so a failure degrades gracefully to null + error note.
"""
import datetime, json, os, subprocess, sys, urllib.request

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage.json")
TENANCY = "ocid1.tenancy.oc1..aaaaaaaavonvqhvqtpx4td7h3mar5hfybifcx2ywoeabezpclmhqornoqpva"
ZONE = "f19d57a918b051ad88892e6b79c10562"

def load_env():
    env = {}
    for p in ["/home/ubuntu/.hermes/.env", "/home/ubuntu/.env"]:
        if os.path.exists(p):
            for line in open(p):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line and not line.startswith("export "):
                    k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"\'')
    return env

ENV = load_env()

def http_json(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

def curl_json(url, auth, timeout=60):
    r = subprocess.run(
        f'curl -s --max-time {timeout} -H "Authorization: {auth}" "{url}"',
        shell=True, capture_output=True, text=True)
    try: return json.loads(r.stdout)
    except: return {"_raw": r.stdout[:200]}

OCI = os.environ.get("OCI_CLI", "/home/ubuntu/bin/oci")

def oci(cmd):
    r = subprocess.run(f"SUPPRESS_LABEL_WARNING=True {OCI} {cmd}", shell=True,
                       capture_output=True, text=True, timeout=120)
    try: return json.loads(r.stdout)
    except Exception: return None

# ==================== ORACLE ====================
def collect_oracle():
    try:
        inst = oci(f"compute instance list --compartment-id {TENANCY} --output json")
        running = 0; ocpus = 0.0; ram = 0.0
        if inst and inst.get("data"):
            for i in inst["data"]:
                if i.get("lifecycle-state") == "RUNNING":
                    running += 1
                    # query per-instance shape-config
                    d = oci(f'compute instance get --instance-id {i["id"]} --output json')
                    if d and d.get("data"):
                        sc = d["data"].get("shape-config", {})
                        ocpus += float(sc.get("ocpus") or 0)
                        ram += float(sc.get("memory-in-gbs") or 0)
        # boot volumes
        boot_tot = 0
        bv = oci(f"bv boot-volume list --compartment-id {TENANCY} --output json")
        if bv and bv.get("data"):
            boot_tot = sum(v.get("size-in-gbs", 0) for v in bv["data"])
        # block volumes
        blk_tot = 0
        blk = oci(f"bv volume list --compartment-id {TENANCY} --output json")
        if blk and blk.get("data"):
            blk_tot = sum(v.get("size-in-gbs", 0) for v in blk["data"])
        return {
            "compute": {"used": ocpus, "limit": 4.0, "unit": "OCPU"},
            "ram":     {"used": ram,   "limit": 24.0, "unit": "GB"},
            "storage": {"used": boot_tot + blk_tot, "limit": 200.0, "unit": "GB"},
            "instances_running": running,
        }
    except Exception as e:
        return {"error": str(e)}

# ==================== CLOUDFLARE ====================
def collect_cloudflare():
    try:
        cft = ENV.get("CF_WORKERS_TOKEN", "")
        acct = ENV.get("CF_ACCOUNT_ID", "")
        out = {}
        # workers scripts count
        d = curl_json(f"https://api.cloudflare.com/client/v4/accounts/{acct}/workers/scripts", f"Bearer {cft}")
        if d and d.get("success"):
            out["workers_scripts"] = {"used": len(d.get("result", [])), "limit": 100, "unit": "scripts"}
        # R2 buckets
        d2 = curl_json(f"https://api.cloudflare.com/client/v4/accounts/{acct}/r2/buckets", f"Bearer {cft}")
        if d2 and d2.get("success"):
            out["r2_buckets"] = {"used": len(d2.get("result", [])), "limit": 100, "unit": "buckets"}
        # pages projects count (free plan limit = 100)
        dp = curl_json(f"https://api.cloudflare.com/client/v4/accounts/{acct}/pages/projects", f"Bearer {cft}")
        if dp and dp.get("success"):
            used = (dp.get("result_info") or {}).get("total_count") or len(dp.get("result") or [])
            out["pages_projects"] = {"used": used, "limit": 100, "unit": "projects"}
        # page rules quota (from zone meta)
        d3 = curl_json(f"https://api.cloudflare.com/client/v4/zones/{ZONE}", f"Bearer {cft}")
        if d3 and d3.get("success"):
            meta = d3["result"].get("meta", {})
            out["page_rules"] = {"used": 0, "limit": meta.get("page_rule_quota", 3), "unit": "rules"}
            out["custom_certs"] = {"used": 0, "limit": meta.get("custom_certificate_quota", 0), "unit": "certs"}
        return out or {"error": "no CF data"}
    except Exception as e:
        return {"error": str(e)}

# ==================== GOOGLE DRIVE ====================
def collect_drive():
    try:
        tok = json.load(open("/home/ubuntu/.hermes/google_token.json"))
        req = urllib.request.Request(
            tok["token_uri"],
            data=json.dumps({"refresh_token": tok["refresh_token"],
                             "client_id": tok["client_id"],
                             "client_secret": tok["client_secret"],
                             "grant_type": "refresh_token"}).encode(),
            headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        at = resp.get("access_token")
        if not at: return {"error": "drive refresh fail"}
        d = http_json("https://www.googleapis.com/drive/v3/about?fields=storageQuota,user",
                      {"Authorization": f"Bearer {at}"})
        q = d["storageQuota"]
        return {
            "storage": {"used": int(q.get("usage", 0)), "limit": int(q.get("limit", 0)), "unit": "bytes"},
            "account": d.get("user", {}).get("emailAddress"),
        }
    except Exception as e:
        return {"error": str(e)}

# ==================== GITHUB ====================
def collect_github():
    try:
        gh = ENV.get("GITHUB_TOKEN", "")
        d = curl_json("https://api.github.com/user", f"token {gh}")
        plan_used = 0  # repo storage
        repos = curl_json("https://api.github.com/user/repos?per_page=100&type=all&sort=updated", f"token {gh}")
        if isinstance(repos, list):
            plan_used = sum(r.get("size", 0) for r in repos) * 1024  # KB -> bytes
        plan_space = d.get("plan", {}).get("space", 0) if d else 0
        return {
            "storage": {"used": plan_used, "limit": plan_space or 976562499, "unit": "bytes"},
            "repos": len(repos) if isinstance(repos, list) else 0,
            "actions_limit_min": 2000,  # GitHub free = 2000 min/month (not API-queryable)
        }
    except Exception as e:
        return {"error": str(e)}

# ==================== BUILD ====================
def git_push():
    """Commit & push usage.json to GitHub Pages repo (used by cron)."""
    try:
        gh = ENV.get("GITHUB_TOKEN", "")
        remote = f"https://forumdata-collab:{gh}@github.com/forumdata-collab/usage-dashboard.git"
        base = os.path.dirname(os.path.abspath(__file__))
        for c in [
            ["git", "-C", base, "add", "usage.json"],
            ["git", "-C", base, "commit", "-m", f"data: usage snapshot {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')}"],
        ]:
            subprocess.run(c, capture_output=True, text=True, timeout=30)
        r = subprocess.run(["git", "-C", base, "push", "-q", remote, "main"],
                           capture_output=True, text=True, timeout=120)
        return "pushed" if r.returncode == 0 else f"push fail: {r.stderr[:200]}"
    except Exception as e:
        return f"git error: {e}"

def main():
    result = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "oracle": collect_oracle(),
        "cloudflare": collect_cloudflare(),
        "drive": collect_drive(),
        "github": collect_github(),
    }
    with open(OUT, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("usage.json written")
    if "--push" in sys.argv:
        print("git:", git_push())

if __name__ == "__main__":
    main()