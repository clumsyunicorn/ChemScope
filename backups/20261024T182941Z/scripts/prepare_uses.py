"""Create source-backed searchable chemical-use records, not invented descriptions."""
from __future__ import annotations
import pandas as pd
from pipeline_common import PROCESSED, save_csv, section

# ------------------------------------
# SOURCE-DERIVED USE DESCRIPTIONS

FILES={'Food':'chemscope_food_regulations.csv','Cosmetics':'chemscope_cosmetics_regulations.csv',
       'Drinking Water':'chemscope_drinking_water_regulations.csv'}


def main():
    section('Chemical uses and product applications')
    entries=[]
    for domain,file in FILES.items():
        p=PROCESSED/file
        if not p.exists():continue
        df=pd.read_csv(p,dtype=str,low_memory=False).fillna('')
        for _,r in df.iterrows():
            application=r.get('product_category',r.get('food_category',''))
            function=r.get('purpose','')
            if not function and not application:continue
            entries.append({'chemical_id':r['chemical_id'],'chemical_name':r['chemical_name'],
                            'domain':domain,'jurisdiction':r['jurisdiction'],
                            'source_use_or_function':function,'source_product_application':application,
                            'source_record_name':r.get('source_record_name',''),
                            'source_id':r.get('source_id',''), 'source_url':r.get('source_url',''),
                            'extraction_method':r.get('extraction_method',''),
                            'plain_language_description':'',
                            'description_status':'Requires plain-language translation and review',
                            'source_record_text':r.get('source_record_text','')})
    fields=['chemical_id','chemical_name','domain','jurisdiction','source_use_or_function',
            'source_product_application','source_record_name','source_id','source_url',
            'extraction_method','plain_language_description','description_status','source_record_text']
    result=pd.DataFrame(entries,columns=fields).drop_duplicates()
    save_csv(result,'chemscope_chemical_uses.csv')
    print('Use records:',len(result),'Plain-language explanations are deliberately unverified until reviewed.')

if __name__=='__main__':main()
