import unittest
import fullbook_llm_v2 as v2
import fullbook_v5_structure as structure


class SectionTests(unittest.TestCase):
    def test_appended_report_not_main_dictionary_entries(self):
        text='## Appendix 1\n\n## A technical report\n\n## INTRODUCTION\n\nThis report evaluates the available sensors.\n\n## SUMMARY\n\nFindings follow.\n\n## Sensors\n\nA sensor detects signals.\n\n## Appendix 2\n\n## Glossary\n\n## ALPHA\n'
        rows=structure.annotate(v2.structural_units(text))
        sensor=next(r for r in rows if r['text'].strip()=='## Sensors')
        alpha=next(r for r in rows if r['text'].strip()=='## ALPHA')
        self.assertEqual(sensor.get('excluded_zone'),'appended_report')
        self.assertFalse(alpha.get('excluded_zone'))

    def test_appendix_alone_is_not_report_evidence(self):
        rows=structure.annotate(v2.structural_units('## Appendix 1\n\n## Glossary\n\n## SENSOR\n\nA device.\n'))
        self.assertFalse(any(r.get('excluded_zone')=='appended_report' for r in rows))

    def test_same_topic_secondary_synonym_subheads_are_not_peers(self):
        prefix='\n\n'.join('## MAINWORD'+str(i) for i in range(22))+'\n\n'
        text=prefix+'## FIGURE\n\nA main entry.\n\n## Figure or Form of the Earth.\n\nA subsection.\n\n## FIGURE IN THEOLOGY\n\nDiscussion.\n\n## Figure, Figurative, Allegorical, Mystical, Typical\n\nMore.\n\n## FINAL CAUSES\n'
        rows=structure.annotate(v2.structural_units(text))
        sub=[r for r in rows if r['text'].startswith('## Figure')]
        self.assertEqual(len(sub),2)
        self.assertTrue(all(r.get('subtitle_evidence')=='same_topic_secondary_style' for r in sub))

    def test_plain_person_comma_name_is_not_a_synonym_list(self):
        prefix='\n\n'.join('## MAINWORD'+str(i) for i in range(22))+'\n\n'
        rows=structure.annotate(v2.structural_units(prefix+'## SMITH\n\nA name.\n\n## Smith, John\n\nA writer.\n'))
        self.assertFalse(rows[-2].get('subtitle_evidence'))


if __name__=='__main__':unittest.main()
