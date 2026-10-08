"""O5 Presenter: answer first, layout fits the content, evidence quoted, Sherlock's view
apart, the question last - and nothing changes in transit."""

from __future__ import annotations

from sherlocks.evidence.case_file import CaseFile
from sherlocks.linkgraph.presenter import VIEW_MARK, format_guard, present


def test_answer_first_question_last_view_apart():
    checked = "Kya waqia 01-03-2023 ko hua?\nKamran 5 FIRs mein naamzad hai [F4]."
    out = present(checked, language="roman", question="Kya waqia 01-03-2023 ko hua?", view="Do haaliya FIRs se jurm barh raha hai.")
    lines = out.splitlines()
    assert lines[0] == "**Kamran 5 FIRs mein naamzad hai [F4].**"
    assert lines[-1] == "ASK:orange: Kya waqia 01-03-2023 ko hua?"          # last, in its colour
    assert lines[-2].startswith(f"{VIEW_MARK} Sherlock ka andaza (tasdeeq nahi):")


def test_three_firs_become_a_table_only_when_nothing_is_lost():
    rows = [{"label": f"{n}/2023", "ps": "Gulberg", "roles": ["accused"], "crimes": ["robbery"], "occurred": "2023-03-01",
             "sources": ["PSRMS"]} for n in (45, 46, 47)]
    checked = "Kamran is named in 3 FIRs:\n" + "\n".join(f"• FIR {r['label']}, PS Gulberg: robbery (accused) [PSRMS]" for r in rows)
    out = present(checked, language="en", facts={"type": "cases", "rows": rows})
    assert "| FIR | Police station | Role | Crime | Date | Source |" in out and "• FIR" not in out
    # A bullet the table would not carry (a section number): the bullets stay.
    lossy = checked + "\n• FIR 48/2023 u/s 392 PPC [PSRMS]"
    kept = present(lossy, language="en", facts={"type": "cases", "rows": rows})
    assert "• FIR 48/2023 u/s 392 PPC" in kept and "| FIR |" not in kept


def test_evidence_quoted_from_the_record_with_its_source():
    case = CaseFile()
    doc = case.add_document(kind="fir", key="fir:1", title="FIR 488/25 · PS Gulberg", source="PSRMS",
                            text="ملزم نے پستول سے فائر کیا")
    fid = case.add_fact(doc, "The accused fired with a pistol", "ملزم نے پستول سے فائر کیا", page=2)
    out = present(f"The accused fired with a pistol [{fid}].", language="en", case=case)
    assert f'> "ملزم نے پستول سے فائر کیا" - FIR 488/25 · PS Gulberg, page 2 [{fid}]' in out


def test_format_guard_catches_a_changed_number():
    assert format_guard("Kamran is in 5 FIRs [F4], CNIC 42101-1234567-1", "Kamran is in 5 FIRs [F4], CNIC 42101-1234567-1") == []
    assert format_guard("Kamran is in 5 FIRs [F4]", "Kamran is in 6 FIRs [F4]") == ["5"]


def test_an_acknowledgement_is_not_bolded():
    assert present("Note kar liya: main suspect.", language="roman", answer_first=False) == "Note kar liya: main suspect."


def test_a_paragraph_becomes_points_labelled_from_its_own_words():
    from sherlocks.linkgraph.presenter import point_label

    case = CaseFile()
    for i in range(2):
        case.add_document(kind="fir", key=f"k{i}", title=f"Doc {i}", source="x", text="t")
    paragraph = ("Sure, let's break down the case. He is the complainant in FIR 121/25 at PS Karampur [PSRMS]. "
                 "The charges are 302 and 148, which cover murder and rioting [PSRMS]. The victim is his daughter, "
                 "about 18 years old D1. Forensic reports confirm six empties from one 7.62mm firearm D2.")
    out = present(paragraph, language="en", case=case)
    lines = out.splitlines()
    assert lines[0] == "**He is the complainant in FIR 121/25 at PS Karampur [PSRMS].**"     # no "Sure, ..."
    assert "• **Charges:** 302 and 148, which cover murder and rioting [PSRMS]." in lines
    assert "• **Victim:** His daughter, about 18 years old [D1]." in lines                 # bare id -> source
    assert any(ln.startswith("• **Forensic reports:**") and "[D2]" in ln for ln in lines)
    # No fixed list: the label is whatever the point is about; none when there is no clear subject.
    assert point_label("The motive was a land dispute [D1].") == ("Motive", "A land dispute [D1].")
    assert point_label("He fled before the police arrived.")[0] is None


def test_the_models_own_point_labels_are_kept_and_bolded():
    out = present("Dad Muhammad has one FIR.\n• Weapon: a 7.62mm firearm [D3]\n• Motive: a land dispute [D1]",
                  language="en")
    assert "• **Weapon:** a 7.62mm firearm [D3]" in out and "• **Motive:** a land dispute [D1]" in out


def test_graph_analysis_lines_are_not_quoted_as_records():
    case = CaseFile()
    doc = case.add_document(kind="graph", key="graph:analysis", title="Graph analysis", source="rules",
                            text="Close to a criminal (stated): Dad is 1 hop from Shaukat.")
    fid = case.add_fact(doc, "Close to a criminal (stated): Dad is 1 hop from Shaukat.",
                        "Close to a criminal (stated): Dad is 1 hop from Shaukat.", by="graph")
    assert "> " not in present(f"Dad is close to a criminal [{fid}].", language="en", case=case)
