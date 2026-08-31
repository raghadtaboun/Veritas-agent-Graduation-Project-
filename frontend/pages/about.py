"""
frontend/pages/about.py — About page (عن النظام).

A non-technical explanation of the Veritas Agent system, its bias labels,
and the confidence score, derived from blueprint.md, plan.md, readme.md,
and agents/bias_agent.py.
"""

from __future__ import annotations

import streamlit as st

from frontend.components import page_header


def render(refresh: bool, last_run: str | None) -> None:
    """Render the About page."""
    page_header("عن النظام", last_run)

    st.markdown(
        """
        <div style="font-family: var(--vt-serif); font-size: 1.15rem; line-height: 1.8; color: var(--vt-ink); max-width: 800px; margin: 0 auto; text-align: right; direction: rtl;">
        
        <h3 style="margin-top: 2rem; border-bottom: 1px solid var(--vt-line); padding-bottom: 0.5rem;">ما هو Veritas Agent؟</h3>
        
        <p>
        <strong>Veritas Agent</strong> هو نظام آلي يعمل بالذكاء الاصطناعي لجمع الأخبار العربية، تحليلها، وكشف الانحياز السياسي فيها. 
        يقوم النظام بجمع المقالات الإخبارية من مصادر متعددة، ثم يجمع المقالات التي تتحدث عن <strong>نفس الحدث</strong> في مكان واحد. 
        بعد ذلك، يقرأ النظام هذه المقالات معاً ويقارن بينها ليكتشف كيف قامت كل وسيلة إعلامية بـ "تأطير" الخبر (أي ما هي الكلمات التي استخدمتها، ومَن هي المصادر التي اقتبست منها، وما هي الحقائق التي ركزت عليها أو تجاهلتها).
        </p>
        <p>
        الهدف من النظام هو زيادة الشفافية الإعلامية ومساعدة القارئ على رؤية الصورة الكاملة للأحداث خارج "فقاعة الفلاتر" (Filter Bubble) من خلال اقتراح مقالات تقدم وجهات نظر مختلفة.
        </p>

        <h3 style="margin-top: 2.5rem; border-bottom: 1px solid var(--vt-line); padding-bottom: 0.5rem;">عناوين التحيز (Bias Labels)</h3>
        
        <p>
        بدلاً من استخدام التصنيفات السياسية الغربية (مثل يمين ويسار) التي لا تتناسب مع الإعلام العربي، يستخدم النظام خمسة تصنيفات مخصصة للسياق العربي:
        </p>
        
        <ul style="margin-bottom: 1.5rem; padding-right: 1.5rem;">
            <li style="margin-bottom: 0.5rem;">
                <strong style="color: #3B82F6;">مؤيد للحكومة (pro_government):</strong> 
                مقال يدعم أو يعزز موقف السلطة الرسمية. غالباً ما يقتبس من المتحدثين الرسميين، يركز على الاستقرار، ويقلل من شأن المعارضة.
            </li>
            <li style="margin-bottom: 0.5rem;">
                <strong style="color: #F59E0B;">معارض (opposition):</strong> 
                مقال ينتقد السلطة. يركز على إخفاقات الحكومة، يقتبس من المعارضين، ويبرز المطالبة بالمساءلة.
            </li>
            <li style="margin-bottom: 0.5rem;">
                <strong style="color: #EF4444;">قومي عربي (pan_arab):</strong> 
                مقال يعكس السردية القومية العربية. يصوّر الأحداث كصراع عربي ضد قوى أجنبية، ويثمن المقاومة.
            </li>
            <li style="margin-bottom: 0.5rem;">
                <strong style="color: #8B5CF6;">متوافق مع الغرب (western_aligned):</strong> 
                مقال يعكس السرديات المؤسساتية الغربية. يركز على حقوق الإنسان والديمقراطية، ويستشهد بكثافة بالمنظمات الغربية.
            </li>
            <li style="margin-bottom: 0.5rem;">
                <strong style="color: #9CA3AF;">محايد (neutral):</strong> 
                مقال متوازن ينقل الحقائق دون لغة تقييمية أو تأطير حزبي، ويستشهد بوجهات نظر متعددة بشكل متناسب.
            </li>
        </ul>

        <h3 style="margin-top: 2.5rem; border-bottom: 1px solid var(--vt-line); padding-bottom: 0.5rem;">ما هو الرقم الموجود بجانب التحيز؟ (درجة الانحياز)</h3>
        
        <p>
        الرقم الذي تراه بجانب شريط التحيز (مثلاً <code>+0.8</code> أو <code>+0.4</code>) يمثل <strong>درجة الانحياز</strong> أو مدى وضوح وقوة هذا الانحياز في المقال.
        </p>
        <p>
        يقوم الذكاء الاصطناعي بتقييم درجة الانحياز من 0.0 إلى 1.0:
        </p>
        <ul style="margin-bottom: 2rem; padding-right: 1.5rem;">
            <li><strong>0.9 إلى 1.0:</strong> انحياز شديد الوضوح. المقال يتبنى هذا الموقف السياسي بشكل صريح وقوي جداً (في المفردات والمصادر).</li>
            <li><strong>0.7 إلى 0.89:</strong> انحياز واضح. الميل السياسي ظاهر ومسنود بأدلة واضحة في النص.</li>
            <li><strong>0.5 إلى 0.69:</strong> انحياز متوسط. يوجد ميل سياسي لكنه قد يكون ضمنياً أو جزئياً.</li>
        </ul>
        <p>
        <em>ملاحظة: إذا كانت درجة الانحياز منخفضة جداً، فإنه يتم تصنيف المقال على أنه "محايد" لعدم وجود أدلة كافية على انحيازه.</em>
        </p>

        <h3 style="margin-top: 2.5rem; border-bottom: 1px solid var(--vt-line); padding-bottom: 0.5rem;">عن المشروع</h3>
        <p>
        تم بناء وتطوير هذا النظام من قِبل <strong>رغد مفتاح محمد طابون</strong> كمشروع تخرج من قسم الهندسة الكهربائية والإلكترونية.
        </p>

        </div>
        """,
        unsafe_allow_html=True,
    )
