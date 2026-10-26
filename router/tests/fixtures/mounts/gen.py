"""Generate the two synthetic ctx.node.v2 mounts (20 nodes each). No real software or game corpus is used."""
import hashlib, json, os, random, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SW = "parser lexer buffer allocator scheduler mutex socket codec cache index query planner compiler linker loader tokenizer renderer pipeline queue tracer".split()
VG = "sprite tilemap collider physics camera shader mesh skeleton animation inventory quest dialogue spawner pathfinding terrain particle audio lighting level boss".split()


def node(corpus, k, word, other, rnd):
    i = "%s-%03d" % (corpus.upper(), k)
    anchors = [{"a": "sec-%d" % k, "role": "section", "intent": "find_method"}]
    if k == 1:
        anchors = [{"a": "uid://example0001", "role": "asset"}, {"a": "scene/Main/Node%d" % k, "role": "scene"}] + anchors
    return {"schema": "ctx.node.v2", "id": i, "corpus": corpus, "kind": "asset" if k == 1 else "symbol", "family": "%s-F%d" % (corpus.upper(), k % 4),
            "title": "%s %s module" % (word.title(), other), "purpose": "Implements %s handling for %s with deterministic %s." % (word, other, word),
            "anchors": anchors,
            "search": {"triggers": ["%s-%s" % (word, other), "%s-handler" % word], "keywords": [word, other, "synthetic"],
                       "problem_names": [other], "tasks": ["Implement a %s for %s" % (word, other)], "questions": ["How does the %s manage %s?" % (word, other)],
                       "exclusions": ["unrelated %s" % rnd.choice(["audio", "network", "database"])]},
            "domains": [{"domain": "software", "strength": "moderate"}], "status": "active",
            "source_sha256": hashlib.sha256(i.encode()).hexdigest()}


def main():
    for corpus, words in (("sw", SW), ("vg", VG)):
        rnd = random.Random(corpus)
        with open(os.path.join(HERE, corpus, "nodes.jsonl"), "w", encoding="utf-8", newline="\n") as f:
            for k in range(1, 21):
                f.write(json.dumps(node(corpus, k, words[k - 1], words[(k * 7) % 20], rnd), sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
