# On-device test: Mac on a VOXI personal hotspot

The automated tests prove that the firewall and the app do what they say. They cannot
prove that the network you are on behaves as you expect, or that your social apps keep
working with the rules applied. Only a run on the real hotspot can show that. This
checklist is for that run. Write the results in the table at the end, and send them back
if anything fails.

Carrier policy matters too. Whether VOXI counts social-app traffic as free is set by VOXI,
not by this app. Check your plan's terms, and check the usage meter on the iPhone at the
end of the test.

## 0. Before you start

- Put the iPhone on Personal Hotspot and join it from the Mac over Wi-Fi.
- Note the iPhone's mobile data usage now (Settings → Mobile Data), so you can compare later.
- Keep a terminal open next to the app. If something you need is blocked, `sudo python3
  hotspot_guard.py disable` turns it off at once.

## 1. What kind of network is this?

```sh
ifconfig | grep -E "inet6|inet " | grep -v "127.0.0.1\|::1"
dig +short AAAA ipv4only.arpa
dig +short A example.com
```

- `ipv4only.arpa` returning a `64:ff9b::` address means the network uses NAT64, so the
  engine adds NAT64 addresses to the allowlist automatically. Note the prefix.
- An `inet6` address that is not `fe80::` means the hotspot gives you IPv6.
- Record which of these you see, and the prefix if there is one.

## 2. What would be allowed?

```sh
cd tools/hotspot-guard
python3 hotspot_guard.py plan
```

- Read the list. Every name that has an address is allowed once blocking is on.
- Every name listed as `no address` stays blocked. Social-media photo and video CDNs
  are the usual cases. Note which ones, because that is where the apps are likely to break.

## 3. Turn blocking on, then test the basics

Turn blocking on in the app (or `sudo python3 hotspot_guard.py watch`).

```sh
curl -sS --max-time 8 -o /dev/null -w "%{http_code}\n" https://api.anthropic.com   # should answer
curl -sS --max-time 8 -o /dev/null -w "%{http_code}\n" https://example.com         # should time out
```

- The first should print an HTTP code (401 or 404 is fine). The second should time out.

## 4. Use the social apps for 10 minutes

Open each app and do the things you normally do:

- [ ] Instagram: feed loads, photos show, a reel plays
- [ ] Facebook: feed loads, photos show, a video plays
- [ ] WhatsApp: messages send and receive, and media downloads
- [ ] X / Threads: timeline loads, images show
- [ ] TikTok: feed loads and videos play
- [ ] Reddit: feed loads, thumbnails show
- [ ] Anything else you use daily (note it below)

For anything that fails, note the app and what did not load. In the app, turn the
group off and on again, or run `plan`, and look for the hostnames involved.

## 5. Check the data meter

- Compare the iPhone's mobile data usage with step 0.
- Expected: most of the background traffic on the Mac is gone, and social-app traffic
  is the only meaningful use. VOXI decides whether that social traffic is free.

## 5a. Check the Data Usage page

- Open **Data Usage** in the app, with blocking on for at least 10 minutes.
- Compare its "All traffic" total with the iPhone's usage change from step 0.
  The two should be close. A large gap means the Mac is not the only device on the
  hotspot, or the carrier counts differently.
- Note the social-media share, and whether the "Excluding social media" view looks right.
  Record "not available" if the page says the split is unavailable.

## 6. Turn blocking off and confirm normal networking

- Turn blocking off in the app (or `sudo python3 hotspot_guard.py disable`).
- Run the first `curl` again. It should answer.
- `sudo nft list tables` on Linux, or the app's status panel on macOS, should show no rules.

## Results

| Check | Result | Notes |
| --- | --- | --- |
| Network type (step 1) | | IPv6? NAT64 prefix? |
| `plan` names with no address | | |
| api.anthropic.com answers while blocking | | |
| example.com times out while blocking | | |
| Instagram | | |
| Facebook | | |
| WhatsApp | | |
| X / Threads | | |
| TikTok | | |
| Reddit | | |
| Data meter change (step 5) | | MB before / after |
| Data Usage page vs iPhone meter (5a) | | All traffic MB / iPhone MB |
| Social-media share on the usage page | | % (or "not available") |
| Normal networking after disable | | |

If any step fails, send back: the table above, the output of `python3 hotspot_guard.py
status --json` and `plan --json`, the output of the `dig` and `ifconfig` commands from
step 1, and the names of the apps and what did not load.
