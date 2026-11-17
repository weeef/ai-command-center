"""Memory evaluation: does recall bring back the right memories, and only those?

    python bench\\memeval.py            run, print the tables
    python bench\\memeval.py --grid 25  show more rows of the threshold grid

A fixed, invented user (nothing personal): 49 memories, 8 earlier chats, 58 questions and 25 pairs of sentences for
the duplicate check. It uses the command center's own Store, Embedder and Memory.recall with the real embedding
model, in a temporary folder, with its own embedding server on port 8192 (CPU only), so the real memory and a
running command center are not touched.

What is measured
  recall      share of the memories a question needs that were brought back
              direct = the question asks about the thing; implicit = the memory matters although nobody asked for it
  noise       unrelated memories brought back per question (memories on a related subject are not counted)
  quiet       share of questions with nothing personal in them ("why is the sky blue") that brought back nothing
  duplicates  similarity of sentences that say the same thing against sentences that only look alike

The grid replays recall's scoring (cosine similarity, + a bonus for a keyword hit, a floor, at most k) for other
values, so thresholds can be chosen from numbers. The first line checks that this replay and Memory.recall agree.
"""
import argparse
import os
import shutil
import socket
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "command-center"))
import memory  # noqa: E402

PORT = 8192

# key, topic, text, subjects (a memory on one of a question's subjects is "related", not noise)
MEMORIES = [
    ("job", "you", "The user works as a backend engineer at a logistics startup called Parcelwise.", "work"),
    ("lang", "you", "The user writes mostly Go at work and Python for personal projects.", "work code"),
    ("city", "you", "The user lives in Tucson, Arizona.", "home"),
    ("tz", "you", "The user's time zone is Mountain Standard Time and does not observe daylight saving.", "home"),
    ("pc", "you", "The user's desktop PC has an RTX 4070 Ti with 12 GB of VRAM and 32 GB of RAM.", "hw"),
    ("laptop", "you", "The user's laptop is a Framework 13 running Fedora.", "hw"),
    ("os", "you", "The user's desktop runs Windows 11 with WSL2 for development.", "hw code"),
    ("keyboard", "you", "The user types on a split ergonomic keyboard with the Colemak layout.", "hw"),
    ("allergy", "you", "The user is allergic to peanuts.", "food health"),
    ("veg", "you", "The user has been vegetarian since 2021.", "food"),
    ("coffee", "you", "The user drinks decaf only, because caffeine gives them migraines.", "food health"),
    ("knee", "you", "The user had knee surgery on 2026-03-12 and is not allowed to run until cleared by the physiotherapist.", "health"),
    ("brief", "preference", "The user wants short answers without introductions or summaries.", "style"),
    ("metric", "preference", "The user prefers metric units.", "style"),
    ("tabs", "preference", "The user prefers tabs over spaces and a 100-column line limit in code.", "code"),
    ("tests", "preference", "The user wants code examples to include a test using the standard library's testing package, not third-party frameworks.", "code"),
    ("dark", "preference", "The user prefers dark themes with the Gruvbox palette.", "ui"),
    ("music", "preference", "The user likes ambient and post-rock music while working, especially Hammock and Explosions in the Sky.", "music"),
    ("books", "preference", "The user enjoys hard science fiction and disliked the characters in The Three-Body Problem.", "books"),
    ("noemoji", "preference", "The user dislikes emoji in replies.", "style"),
    ("sprout", "project", "The user is building a self-hosted plant-watering tracker called Sproutlog with a Go API and SQLite.", "sprout code"),
    ("sprout2", "project", "Sproutlog reads soil-moisture sensors over MQTT from ESP32 boards.", "sprout"),
    ("migrate", "project", "At work the user is migrating Parcelwise's route planner from a monolith to gRPC services, due by 2027-01-15.", "work"),
    ("novella", "project", "The user is writing a science-fiction novella set on a tidally locked planet, working title 'Terminator Line'.", "writing"),
    ("spanish", "project", "The user is learning Spanish, is at roughly A2 level and practises 20 minutes each morning.", "spanish"),
    ("hike", "project", "The user plans to hike Passage 11 of the Arizona Trail once their knee has healed.", "health hike"),
    ("garden", "project", "The user grows chiles and tomatoes in raised beds and starts seeds indoors in February.", "garden"),
    ("partner", "people", "The user's partner is named Noor and works as a pediatric nurse on night shifts.", "family"),
    ("dog", "people", "The user's dog is a greyhound named Biscuit who is afraid of thunderstorms.", "pets"),
    ("cat", "people", "The user's cat is named Pepper and needs a daily thyroid pill.", "pets"),
    ("sister", "people", "The user's sister Imani lives in Lisbon and has a birthday on 9 August.", "family"),
    ("boss", "people", "The user's manager at Parcelwise is Dariusz, who prefers written design docs over meetings.", "work"),
    ("friend", "people", "The user's friend Tomasz runs the weekly board-game night on Thursdays.", "friends"),
    ("mom", "people", "The user's mother is recovering from a hip replacement and lives in Flagstaff.", "family"),
    ("wifi", "fact", "The user's home network uses a UniFi gateway with a separate VLAN for IoT devices.", "hw net"),
    ("nas", "fact", "The user backs up to a Synology NAS nightly at 02:00 and to Backblaze B2 weekly.", "hw backup"),
    ("car", "fact", "The user drives a 2019 Mazda 3 hatchback.", "car"),
    ("license", "fact", "The user's Sproutlog code is licensed under AGPL-3.0.", "sprout"),
    ("editor", "fact", "The user uses Neovim with the LazyVim distribution.", "code ui"),
    ("db", "fact", "Parcelwise's production database is PostgreSQL 16 on AWS RDS.", "work"),
    ("conf", "event", "The user is speaking at GopherCon on 2027-07-14 about gRPC migration.", "work events"),
    ("trip", "event", "The user and Noor are travelling to Lisbon from 2027-08-05 to 2027-08-16.", "family travel"),
    ("vet", "event", "Biscuit has a dental cleaning at the vet on 2026-12-03.", "pets"),
    ("lease", "event", "The user's apartment lease ends on 2027-05-31.", "home"),
    ("wsl", "howto", "The user fixed WSL2 DNS failures by setting generateResolvConf=false and adding 1.1.1.1 to /etc/resolv.conf.", "hw code"),
    ("esp", "howto", "Flashing the user's ESP32 boards only works at 115200 baud with the BOOT button held.", "sprout"),
    ("bread", "howto", "The user's sourdough works best at 78% hydration with an overnight proof in the fridge.", "food"),
    ("pgtest", "howto", "The user speeds up Postgres test suites by running them against a tmpfs-backed data directory.", "work code"),
    ("migraine", "howto", "A dark room and magnesium help the user's migraines more than ibuprofen.", "health"),
]

# question, needed memories, subjects, kind (d = direct, i = implicit), earlier turns [(user, assistant)]
QUESTIONS = [
    ("what's my dog's name again?", "dog", "pets", "d", None),
    ("Remind me when Biscuit's vet appointment is", "vet", "pets", "d", None),
    ("which GPU do I have? will a 14B model at Q4 fit", "pc", "hw", "d", None),
    ("my wsl can't resolve hostnames again", "wsl", "hw code", "d", None),
    ("how's my Spanish going, give me a quick drill at my level", "spanish", "spanish", "d", None),
    ("the esp32 won't flash, any ideas", "esp", "sprout", "d", None),
    ("what license did I pick for sproutlog", "license", "sprout", "d", None),
    ("when does my lease end", "lease", "home", "d", None),
    ("what are the dates of our Portugal trip", "trip", "family travel", "d", None),
    ("who hosts game night", "friend", "friends", "d", None),
    ("what am I talking about at gophercon", "conf", "work events", "d", None),
    ("when should I start my tomato seeds", "garden", "garden", "d", None),
    ("which car do I drive", "car", "car", "d", None),
    ("what's the deadline for the grpc work", "migrate", "work events", "d", None),
    ("which database does work use in prod", "db", "work", "d", None),
    ("which laptop do I have", "laptop", "hw", "d", None),
    ("does my cat need anything today", "cat", "pets", "d", None),
    ("where do I live", "city", "home", "d", None),
    ("what's my keyboard layout", "keyboard", "hw", "d", None),
    ("how do I back up this new folder too", "nas", "hw backup", "d", None),
    ("how is mom doing, what should I bring when I visit", "mom", "family", "d", None),
    ("what time is it for me if it's 15:00 UTC", "tz", "home", "d", None),
    ("is it going to storm tonight? worried about the dog", "dog", "pets", "d", None),
    ("what was the trick for slow database tests", "pgtest", "work code", "d", None),
    ("what's my sister's birthday", "sister", "family", "d", None),
    ("what should I make for dinner tonight? something quick", "veg allergy", "food health family", "i", None),
    ("can you recommend a trail snack", "allergy", "food health hike", "i", None),
    ("I need a present idea for my sister", "sister", "family travel", "i", None),
    ("write me a function that removes duplicates from a slice", "lang tabs tests", "code work sprout", "i", None),
    ("draft an email to my manager about the migration slipping", "boss migrate", "work events style", "i", None),
    ("suggest a playlist for deep work", "music", "music", "i", None),
    ("recommend a novel for my flight", "books", "books travel writing", "i", None),
    ("plan a workout for this week", "knee", "health hike", "i", None),
    ("should I have an espresso before the meeting?", "coffee", "food health", "i", None),
    ("I have a headache coming on", "migraine coffee", "health food", "i", None),
    ("my bread came out dense", "bread", "food", "i", None),
    ("tests are slow in CI for the route planner database", "pgtest db", "work code", "i", None),
    ("help me name a character for my story", "novella", "writing books", "i", None),
    ("what editor config should I use for this repo", "editor tabs", "code ui", "i", None),
    ("Noor's schedule makes dinner together hard, ideas?", "partner", "family food", "i", None),
    ("how tall is Mount Lemmon", "metric", "style home hike", "i", None),
    ("set up a new IoT sensor on my network", "wifi", "hw net sprout", "i", None),
    ("and the cat?", "cat", "pets", "d", [("When is Biscuit's appointment?", "The dental cleaning at the vet is on 3 December.")]),
    ("how do they talk to the server", "sprout2", "sprout code", "d", [("Help me with sproutlog's sensor code", "Sure, which part?")]),
    # nothing personal: recall should stay quiet
    ("what's the capital of Australia", "", "", "n", None),
    ("explain how TCP's three-way handshake works", "", "", "n", None),
    ("hi", "", "", "n", None),
    ("thanks, that's perfect", "", "", "n", None),
    ("summarise the plot of Hamlet", "", "", "n", None),
    ("who won the 1998 world cup", "", "", "n", None),
    ("what does HTTP status 418 mean", "", "", "n", None),
    ("continue", "", "", "n", None),
    ("how many moons does Jupiter have", "", "", "n", None),
    ("what's 17% of 2340", "", "", "n", None),
    ("tell me a joke", "", "", "n", None),
    ("why is the sky blue", "", "", "n", None),
    ("ok", "", "", "n", None),
    ("what year did the Berlin Wall fall", "", "", "n", None),
]

CHATS = [
    ("mqtt", "Sproutlog MQTT reconnect bug", "The MQTT client in sproutlog drops when the broker restarts and never comes back",
     "Set AutoReconnect and a ConnectRetryInterval on the paho client options, and resubscribe inside the OnConnect handler: with a clean session the broker forgets your subscriptions."),
    ("thunder", "Greyhound and thunder", "Biscuit shakes for hours when there's thunder, what can I do",
     "Try a snug anxiety wrap, white noise in an inner room, and a lick mat as a distraction. If it stays severe, ask the vet about trazodone before storms."),
    ("deadline", "gRPC deadlines", "How should deadlines work across our new grpc services",
     "Set the deadline once at the edge, pass the context through every call so it propagates, and have each service check ctx.Err() before expensive work. Don't set fresh timeouts per hop."),
    ("lisbon", "Lisbon itinerary", "What day trips are worth it from Lisbon",
     "Sintra for the palaces (go early, book Pena in advance), Cascais for the coast by train, and Evora if you want Roman ruins. For pastel de nata, Manteigaria over the tourist queue in Belem."),
    ("leggy", "Leggy chile seedlings", "My chile seedlings are tall and floppy",
     "They are stretching for light. Lower the LED to about 5 cm above the leaves, run a small fan so the stems thicken, and bury the stem deeper when you pot them on."),
    ("winds", "Novella worldbuilding", "How would weather work on my tidally locked planet",
     "Hot air rises on the day side and flows to the night side at altitude, while cold surface winds blow back toward the day side. Settlements in the twilight band would face a constant cold wind from the dark."),
    ("b2", "Hyper Backup to B2 failing", "Hyper Backup says authentication failed for the B2 task",
     "The application key was probably deleted or lost its bucket permission. Create a new key limited to that bucket, re-link the task, and check that the bucket lifecycle keeps only the last version."),
    ("imperfect", "Spanish past tenses", "When do I use the imperfect instead of the preterite",
     "Preterite for completed, one-off actions (ayer comi); imperfect for background, habits and descriptions (cuando era nino, comia). A story usually sets the scene in imperfect and moves in preterite."),
]

CHAT_QUESTIONS = [
    ("what did we figure out about the mqtt reconnect problem", "mqtt"),
    ("what was that thing you suggested for Biscuit during storms", "thunder"),
    ("how did we say deadlines should be handled across the grpc services", "deadline"),
    ("which day trips from Lisbon did we talk about", "lisbon"),
    ("my seedlings are leggy again, what did we do last time", "leggy"),
    ("remind me how the winds work on my planet", "winds"),
    ("hyper backup is failing again", "b2"),
    ("when do I use the imperfect again", "imperfect"),
]

SAME = [   # say the same thing: add_checked should treat the second as the first
    ("The user's dog is a greyhound named Biscuit who is afraid of thunderstorms.", "The user has a greyhound called Biscuit that is scared of thunder."),
    ("The user is allergic to peanuts.", "The user has a peanut allergy."),
    ("The user lives in Tucson, Arizona.", "The user's home is in Tucson, AZ."),
    ("The user prefers metric units.", "The user wants measurements in metric."),
    ("The user's laptop is a Framework 13 running Fedora.", "The user uses a Framework 13 laptop with Fedora Linux."),
    ("The user wants short answers without introductions or summaries.", "The user prefers brief replies with no preamble or recap."),
    ("The user's apartment lease ends on 2027-05-31.", "The lease on the user's apartment runs out on 2027-05-31."),
    ("The user drinks decaf only, because caffeine gives them migraines.", "The user avoids caffeine since it triggers migraines and sticks to decaf."),
    ("The user's manager at Parcelwise is Dariusz, who prefers written design docs over meetings.", "Dariusz, the user's boss at Parcelwise, would rather get design docs in writing than hold meetings."),
    ("The user uses Neovim with the LazyVim distribution.", "The user's editor is Neovim (LazyVim)."),
    ("The user has been vegetarian since 2021.", "The user became a vegetarian in 2021."),
    ("The user drives a 2019 Mazda 3 hatchback.", "The user's car is a Mazda 3 hatchback from 2019."),
]
DIFFERENT = [   # look alike, mean something else. The last four correct an older fact: there, the new sentence
                # replacing the old one is what should happen anyway, so a merge is not a mistake.
    ("The user's dog is a greyhound named Biscuit who is afraid of thunderstorms.", "The user's cat is named Pepper and needs a daily thyroid pill."),
    ("The user is allergic to peanuts.", "The user's partner Noor is allergic to shellfish."),
    ("The user lives in Tucson, Arizona.", "The user's mother lives in Flagstaff, Arizona."),
    ("The user's laptop is a Framework 13 running Fedora.", "The user's desktop runs Windows 11 with WSL2 for development."),
    ("The user's apartment lease ends on 2027-05-31.", "The user's car insurance renews on 2027-05-31."),
    ("Biscuit has a dental cleaning at the vet on 2026-12-03.", "Pepper has a blood test at the vet on 2026-12-10."),
    ("The user writes mostly Go at work and Python for personal projects.", "The user is learning Rust in their spare time."),
    ("The user's sister Imani lives in Lisbon and has a birthday on 9 August.", "The user's friend Tomasz has a birthday on 9 August."),
    ("The user is speaking at GopherCon on 2027-07-14 about gRPC migration.", "The user is attending KubeCon on 2027-11-09."),
    ("The user's apartment lease ends on 2027-05-31.", "The user's apartment lease ends on 2028-05-31."),
    ("The user has been vegetarian since 2021.", "The user has been vegan since 2025."),
    ("The user lives in Tucson, Arizona.", "The user lives in Denver, Colorado."),
    ("The user drinks decaf only, because caffeine gives them migraines.", "The user drinks two cups of regular coffee a day."),
]


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1))))] if xs else float("nan")


def messages(q, prev):
    out = []
    for u, a in prev or []:
        out += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
    return out + [{"role": "user", "content": q}]


def query_text(q, prev):
    """Exactly what Memory.recall embeds."""
    msgs = messages(q, prev)
    ctx = " ".join(m["content"][:300] for m in msgs[-3:-1])
    return (ctx + "\n" + q)[-2000:]


def pick(sims, kw, floor, bonus, k, margin=None):
    """Memory.recall's choice, with its numbers as parameters. sims: [(id, cosine)] best first; kw: keyword hits."""
    scores = dict(sims[:24])
    for mid in kw:
        scores[mid] = scores.get(mid, 0.0) + bonus
    ranked = sorted(scores.items(), key=lambda t: -t[1])
    if margin is not None and ranked:
        floor = max(floor, ranked[0][1] - margin)
    return [mid for mid, s in ranked if s >= floor][:k]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--grid", type=int, default=5, help="rows of the threshold grid to show")
    ap.add_argument("--llama", default=os.path.join(ROOT, "llama.cpp", "llama-server.exe"))
    ap.add_argument("--model", default=os.path.join(ROOT, "models", "Qwen3-Embedding-0.6B-GGUF", "Qwen3-Embedding-0.6B-Q8_0.gguf"))
    a = ap.parse_args()

    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            sys.exit("port %d is in use (another evaluation still running?)" % PORT)
    memory.EMBED_PORT = PORT
    home = tempfile.mkdtemp(prefix="memeval-")
    M = memory.Memory()
    M.settings, M.default_home = {"memory_dir": home}, home
    M.embedder = emb = memory.Embedder(a.llama, a.model)
    if not emb.available():
        sys.exit("embedding model or llama-server not found:\n  %s\n  %s" % (a.model, a.llama))
    M.open_home(home)
    st = M.store
    try:
        t0 = time.time()
        while not emb.ensure():
            if time.time() - t0 > 180:
                sys.exit("the embedding server didn't start")
            time.sleep(0.5)
        ids, subj = {}, {}
        for key, topic, text, subjects in MEMORIES:
            ids[key] = st.add(text, topic, "eval")
            subj[ids[key]] = set(subjects.split())
        name = {v: k for k, v in ids.items()}
        chunk_of = {}
        for key, title, u, asst in CHATS:
            st.put_chunks("eval-" + key, title, [(0, int(time.time()), "User: %s\nAssistant: %s" % (u, asst))])
            chunk_of[st.q("SELECT id FROM chunks WHERE chat_id=?", ("eval-" + key,))[0][0]] = key
        while st.missing_vectors(emb.name, 1):
            M._index_some()
        model = emb.name

        def embed_queries(texts):
            out = []
            for i in range(0, len(texts), 16):
                v = emb.embed(texts[i:i + 16], query=True, timeout=120)
                if not v:
                    sys.exit("embedding failed: %s" % emb.error)
                out += v
            return out

        # ---- questions: raw scores
        qv = embed_queries([query_text(q, prev) for q, _, _, _, prev in QUESTIONS])
        rows = []
        for (q, need, subjects, kind, prev), v in zip(QUESTIONS, qv):
            rows.append({"q": q, "kind": kind, "prev": prev, "need": [ids[k] for k in need.split()], "subjects": set(subjects.split()),
                         "sims": st.similar("m", model, v, len(MEMORIES)), "kw": st.keyword("m", q, 12),
                         "chunks": st.similar("c", model, v, len(CHATS))})

        def measure(floor, bonus, k, margin=None):
            got = {"d": [0, 0], "i": [0, 0]}
            noise, quiet, n_neg, sent = 0, 0, 0, 0
            for r in rows:
                p = pick(r["sims"], r["kw"], floor, bonus, k, margin)
                sent += len(p)
                unrelated = [m for m in p if m not in r["need"] and not (subj[m] & r["subjects"])]
                noise += len(unrelated)
                if r["kind"] == "n":
                    n_neg += 1
                    quiet += not p
                else:
                    got[r["kind"]][0] += sum(1 for m in r["need"] if m in p)
                    got[r["kind"]][1] += len(r["need"])
            return {"direct": got["d"][0] / got["d"][1], "implicit": got["i"][0] / got["i"][1],
                    "noise": noise / len(rows), "quiet": quiet / n_neg, "sent": sent / len(rows)}

        # ---- the replay must agree with the real thing
        agree = 0
        for r in rows:
            _, info = M.recall(messages(r["q"], r["prev"]))
            agree += set(info.get("ids") or []) == set(pick(r["sims"], r["kw"], memory.MEM_MIN, 0.12, 8))
        print("replay agrees with Memory.recall on %d of %d questions" % (agree, len(rows)))
        print("embedding model: %s   memories: %d   chats: %d   questions: %d\n" % (model, len(MEMORIES), len(CHATS), len(rows)))

        # ---- where the scores lie
        need_d = [dict(r["sims"])[m] for r in rows if r["kind"] == "d" for m in r["need"]]
        need_i = [dict(r["sims"])[m] for r in rows if r["kind"] == "i" for m in r["need"]]
        top_n = [r["sims"][0][1] for r in rows if r["kind"] == "n"]
        unrel = [max([s for m, s in r["sims"] if m not in r["need"] and not (subj[m] & r["subjects"])] or [0]) for r in rows if r["kind"] != "n"]
        print("cosine similarity                          lowest   10%    median  90%    highest")
        for label, xs in (("needed memory, direct question", need_d), ("needed memory, implicit question", need_i),
                          ("best unrelated memory, personal question", unrel), ("best memory, nothing-personal question", top_n)):
            print("  %-40s %.2f     %.2f   %.2f    %.2f   %.2f" % (label, min(xs), pct(xs, 10), pct(xs, 50), pct(xs, 90), max(xs)))

        # ---- threshold grid
        def line(tag, floor, bonus, k, margin, m):
            return "  %-9s floor %.2f  keyword +%.2f  k %d  margin %-4s | direct %3.0f%%  implicit %3.0f%%  noise %.2f  quiet %3.0f%%  sent %.1f" % (
                tag, floor, bonus, k, "-" if margin is None else "%.2f" % margin, m["direct"] * 100, m["implicit"] * 100, m["noise"], m["quiet"] * 100, m["sent"])

        cur = measure(memory.MEM_MIN, 0.12, 8)
        print("\nrecall of memories (noise = unrelated memories per question, sent = memories per question)")
        print(line("current", memory.MEM_MIN, 0.12, 8, None, cur))
        for floor in [x / 100.0 for x in range(30, 52, 2)]:
            if abs(floor - memory.MEM_MIN) > 0.001:
                print(line("", floor, 0.12, 8, None, measure(floor, 0.12, 8)))
        print("best of the wider grid (keyword bonus, k and a margin below the best score varied too)")
        grid = []
        for floor in [x / 100.0 for x in range(30, 66, 2)]:
            for bonus in (0.0, 0.06, 0.12):
                for k in (4, 6, 8):
                    for margin in (None, 0.10, 0.15, 0.20):
                        m = measure(floor, bonus, k, margin)
                        # one number to sort by: finding what is needed matters most, then staying quiet, then noise
                        m["score"] = 0.45 * m["direct"] + 0.25 * m["implicit"] + 0.2 * m["quiet"] - 0.1 * min(m["noise"], 1.0)
                        grid.append((m["score"], floor, bonus, k, margin, m))
        grid.sort(key=lambda g: (-round(g[0], 4), g[3], -g[1]))
        for _, floor, bonus, k, margin, m in grid[:a.grid]:
            print(line("", floor, bonus, k, margin, m))

        # ---- missed and noisy questions at the current settings
        print("\nat the current settings")
        for r in rows:
            p = pick(r["sims"], r["kw"], memory.MEM_MIN, 0.12, 8)
            miss = [name[m] for m in r["need"] if m not in p]
            bad = [name[m] for m in p if m not in r["need"] and not (subj[m] & r["subjects"])]
            if miss or bad:
                s = dict(r["sims"])
                print("  %-58s %s%s" % (r["q"][:58], ("missed " + ", ".join("%s %.2f" % (name[m], s[m]) for m in r["need"] if m not in p) + "  ") if miss else "",
                                         ("noise " + ", ".join("%s %.2f" % (b, s[ids[b]]) for b in bad)) if bad else ""))

        # ---- earlier chats
        cq = embed_queries([query_text(q, None) for q, _ in CHAT_QUESTIONS])
        right, wrong = [], []
        for (q, key), v in zip(CHAT_QUESTIONS, cq):
            for cid, s in st.similar("c", model, v, len(CHATS)):
                (right if chunk_of[cid] == key else wrong).append(s)
        other = [r["chunks"][0][1] for r in rows if r["kind"] == "n"]
        personal = [r["chunks"][0][1] for r in rows if r["kind"] != "n"]
        print("\nearlier chats (CHUNK_MIN is %.2f)                lowest   10%%    median  90%%    highest" % memory.CHUNK_MIN)
        for label, xs in (("the chat that was asked about", right), ("other chats, same question", wrong),
                          ("best chat, memory question", personal), ("best chat, nothing-personal question", other)):
            print("  %-40s %.2f     %.2f   %.2f    %.2f   %.2f" % (label, min(xs), pct(xs, 10), pct(xs, 50), pct(xs, 90), max(xs)))
        for t in [x / 100.0 for x in range(35, 66, 5)]:
            print("  CHUNK_MIN %.2f: asked-about chat found %d/%d, wrong chats added %.2f per question, nothing-personal questions that pull a chat %d/%d" % (
                t, sum(s >= t for s in right), len(right), sum(s >= t for s in wrong) / len(CHAT_QUESTIONS), sum(s >= t for s in other), len(other)))

        # ---- duplicates
        def pairs(ps):
            flat = [t for p in ps for t in p]
            v = []
            for i in range(0, len(flat), 16):
                v += emb.embed(flat[i:i + 16], timeout=120)
            return [sum(x * y for x, y in zip(v[i], v[i + 1])) for i in range(0, len(v), 2)]
        same, diff = pairs(SAME), pairs(DIFFERENT)
        print("\nduplicate check (add_checked merges at %.2f)       lowest   10%%    median  90%%    highest" % memory.DUP_MIN)
        print("  %-40s %.2f     %.2f   %.2f    %.2f   %.2f" % ("same meaning", min(same), pct(same, 10), pct(same, 50), pct(same, 90), max(same)))
        print("  %-40s %.2f     %.2f   %.2f    %.2f   %.2f" % ("different meaning", min(diff), pct(diff, 10), pct(diff, 50), pct(diff, 90), max(diff)))
        for t in (0.75, 0.80, 0.85, 0.88, 0.90, 0.93):
            print("  merge at %.2f: %2d of %d duplicates caught, %d of %d look-alikes merged" % (
                t, sum(s >= t for s in same), len(same), sum(s >= t for s in diff), len(diff)))
        worst = sorted(zip(diff, DIFFERENT), reverse=True)[:5]
        for s, (x, y) in worst:
            print("    %.2f  %s | %s" % (s, x[:52], y[:52]))
    finally:
        emb.stop()
        st.close()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()
