"""Code-native product artwork, never a financial result or authorization."""
from .ui import icon


def artwork():
    return '''<figure class="flow-scene" data-depth-scene>
<div class="flow-art" aria-hidden="true"><div class="flow-aura"></div>
<div class="flow-stage" data-depth-stage>
<div class="flow-orbit"></div><span class="flow-spark flow-spark--one"></span><span class="flow-spark flow-spark--two"></span>
<div class="flow-layer flow-layer--evidence"><div class="flow-layer-title"><span class="flow-glyph">'''+icon('file')+'''</span><span>01 / EVIDENCE</span></div><b>Start with the facts.</b><div class="flow-paper"><i></i><i></i><i></i></div></div>
<div class="flow-layer flow-layer--forecast"><div class="flow-layer-title"><span class="flow-glyph">'''+icon('layers')+'''</span><span>02 / FORECAST</span></div><b>See the possibilities.</b><div class="flow-columns"><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div></div>
<div class="flow-layer flow-layer--review"><div class="flow-layer-title"><span class="flow-glyph">'''+icon('check')+'''</span><span>03 / OWNER REVIEW</span></div><b>You choose the next step.</b><span class="flow-review-line"></span></div>
<div class="flow-seal"><img src="/assets/fluxora-mark-v1.png" width="38" height="38" alt=""><span>Fluxora<span class="brand-dot">.</span></span></div>
</div></div><figcaption>Clarity, one layer at a time.<small>Illustrative artwork · no financial data.</small></figcaption></figure>'''
