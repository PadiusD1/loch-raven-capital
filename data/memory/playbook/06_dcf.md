# Intrinsic valuation with a DCF

Firm playbook chapter 6. Written in our own words from a Morgan State University DCF lesson series (April 2026) and the author's Wall Street Prep Sell Side Equity Research Certification notes. The worked numbers are illustrative.

## The idea

A company is worth the present value of the cash it will generate. Project free cash flow for five to ten years, add a terminal value for everything after, and discount both back to today at a rate that reflects risk and the time value of money.

## Unlevered free cash flow

Unlevered free cash flow belongs to all capital providers. Compute it as `EBIT x (1 - tax rate) + D&A - capex - increase in net working capital`. Discount it at the weighted average cost of capital to get enterprise value, then subtract net debt to reach equity value and divide by diluted shares. A levered DCF discounts free cash flow to equity at the cost of equity instead. Always match the cash flow to the rate, firm cash flow with WACC and equity cash flow with the cost of equity.

## The discount rate

The cost of equity comes from CAPM, `risk free rate + beta x equity risk premium`. WACC blends the after tax cost of debt and the cost of equity by their weights in the capital structure, `WACC = E/V x cost of equity + D/V x cost of debt x (1 - tax rate)`. Cheap debt lowers WACC until too much of it raises distress risk. A discount rate that is too low is the fastest way to overvalue a company.

## Terminal value

Terminal value is usually most of the answer, often between half and four fifths of total value. The perpetuity growth method is `FCF next year / (WACC - g)`, and the growth rate must stay at or below long run economic growth. The exit multiple method applies a multiple such as EV to EBITDA to the final year. When the two methods disagree badly, one of the assumptions is wrong.

## Worked example

Take a stable company with 100 of free cash flow growing 4% a year for five years, a 7% WACC and 2% terminal growth. The five discounted cash flows sum to about 460. Terminal value at the end of year five is about 2,482, worth about 1,770 today, so enterprise value is about 2,229 and the terminal value is roughly 79% of it. At a 6% WACC value rises about 25%, and at 8% it falls about 17%. One point of discount rate moves the answer more than most forecast debates.

## Present a range, not a point

Because the output is so sensitive to WACC and terminal growth, always show a sensitivity table across both and report a range. If the DCF says the stock is worth far more than the market price, first suspect your own assumptions, not the market. Conservative analysts shade projections down to build in a margin of safety.

## The main criticism

A DCF is only as good as its assumptions. Ground every input in reported results and industry norms, state what would have to be true for the value to hold, and pair the DCF with relative valuation so the two methods check each other.
