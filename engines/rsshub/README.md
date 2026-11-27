# RSSHub, the bridge to sites without a feed

Many sites publish no feed: a Bilibili space, a 小宇宙 show, a Telegram channel, a Weibo account. [RSSHub](https://github.com/DIYgod/RSSHub) makes feeds of them, each at a route of its own, such as `/bilibili/user/video/946974`. This folder runs RSSHub on your own machine, and infonium subscribes to its routes as it does to any feed.

You need Docker.

```bash
docker compose up -d
```

This starts RSSHub at <http://127.0.0.1:1200>, where infonium looks for it, and starts it again after a restart of the machine until you stop it. The image weighs 124 MiB to download and 638 MB on disk; it has no browser. The running engine held 257 MiB of memory idle, 368 MiB after reading the feeds of 22 routes, and 339 MiB after the addresses of 25 sites besides; under a key, 210, 367 and 322 MiB. It takes no processor time while no one asks it anything.

## Subscribing

Subscribe to a route as `rsshub://` followed by the route: `rsshub://bilibili/user/video/946974`. infonium keeps the subscription as that route, and asks for its feed at the instance that `INFONIUM_RSSHUB_BASE_URL` names at each poll, so that moving RSSHub to another port or machine keeps every subscription. A route is kept written one way: `rsshub://youtube/user/@veritasium` and `rsshub://youtube/user/%40veritasium` are one subscription.

You can also give the site's own address, as `https://t.me/s/durov`. Where infonium finds no feed of the site by its own means, it matches the address against the rules RSSHub publishes for its browser extension, at `/api/radar/rules`, and subscribes to the route of the rule that matches it best, which is kept as a route as above. Where several rules match it equally well, as for a Bilibili space or a GitHub repository's page, infonium subscribes to none of them: it names their routes, twelve at most, and you give the one you want. The rules cover 1,585 sites; of their 6,734 rules, infonium can use 6,473, those whose route follows from a page's address alone.

The routes are listed at <https://docs.rsshub.app/routes/>, each with the site and the address of its own that it stands for.

## Routes that need an account or a key

Some routes read a site only with your account or a key of yours, and RSSHub answers them with an error until it has one; infonium shows that error. The settings each route needs are listed with it in RSSHub's documentation, under names such as `TWITTER_AUTH_TOKEN`. Write them into a file `.env` beside `compose.yaml`, one to a line:

```
TWITTER_AUTH_TOKEN=the token of your account
```

and start the engine again with `docker compose up -d`. git keeps no `.env`.

A few routes, such as Douyin's, need a browser, which this image leaves out; RSSHub answers them with an error. Bilibili's videos of a user asked for one now and then: over six starts of RSSHub, the route gave its feed at some requests and asked for a browser at others.

## Another port

To use another port, name it: `RSSHUB_PORT=1300 docker compose up -d`, and set `INFONIUM_RSSHUB_BASE_URL=http://127.0.0.1:1300` for infonium. The port is published on 127.0.0.1 only.

## On a machine that others use

RSSHub answers whoever reaches its port, and on 127.0.0.1 that is every user and every program on this machine. Without a key, any of them can read any route through it, with the tokens of your accounts that `.env` gives it, as you would. On a machine that others use, give RSSHub a key, and infonium the same key. In `.env`, readable by you alone (`chmod 600 .env`):

```
ACCESS_KEY=a long random key
```

Start the engine again with `docker compose up -d`, and set `INFONIUM_RSSHUB_ACCESS_KEY` to the same key for infonium. RSSHub then answers only a request that carries the key in its query (`?key=`), its rules among them. infonium sends it with every request it makes to RSSHub, at a poll and at a discovery, and shows it in no address, sentence or log. Where infonium gives no key, or another, it shows that RSSHub refused it.

The key goes to the address that `INFONIUM_RSSHUB_BASE_URL` names and to no other server. Where that address redirects to another, as from `http://` to `https://` of one host, and the redirect carries the key, infonium does not follow it and shows where it leads; name that address in the setting.

Where a proxy is set with `HTTP_PROXY` or `ALL_PROXY`, name 127.0.0.1 in `NO_PROXY`, as for MinerU: infonium would otherwise ask RSSHub through the proxy, which would be sent the key and, on another machine, could not reach RSSHub here.

Whoever may use Docker on the machine can read the container's settings, the key among them.

Without a key, put no token of an account into RSSHub on such a machine.

## Stopping it

`docker compose down` stops RSSHub and removes its container. What it fetched is kept in its memory for 5 minutes for a route and an hour for the pages of its postings, and is lost when it stops.

## Licence

RSSHub is under the GNU Affero General Public License 3.0. It is a separate program, which infonium reaches over HTTP; its licence does not extend to infonium.
