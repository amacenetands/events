from src.sources.adapters import get_adapter

HTML = """<html><script>const X=1;
const EVENTS = [
  {d:"2026-11-07",time:"12:00",type:"play",name:"Rabbits' Choice",sub:"Lab, {a:b} x",price:"from £40"},
  {d:"2026-11-08",type:"food",name:"Dinner",sub:"",price:"free",link:"https://e.x/a"},
];
EVENTS.push({d:"2026-12-01",name:"later"});
</script></html>"""
CFG = {"adapter": {"kind": "js_array", "var": "EVENTS", "title": "name", "date": "d", "time": "time",
                   "desc": "sub", "price": "price", "url": "link"}}


def test_reads_literal_array_only():
    recs = get_adapter(CFG).records(HTML, CFG)
    assert [(r.title, r.date, r.time, r.price) for r in recs] == [
        ("Rabbits' Choice", "2026-11-07", "12:00", "from £40"), ("Dinner", "2026-11-08", "", "free")]
    assert recs[0].desc == "Lab, {a:b} x" and recs[1].url == "https://e.x/a"


def test_missing_var():
    assert get_adapter(CFG).records("<script>var y=[]</script>", CFG) == []
