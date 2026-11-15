"""src.normalize.prices — spec cases + strings mined from cache/memory.db."""
import pytest

from src.normalize import find_price, parse_price_text

CASES = [
    ("Free", "Free"), ("£12", "£12"), ("£8 - £30", "£8–£30"), ("£8–£30", "£8–£30"), ("From £15", "From £15"),
    ("Pay what you can", "Pay what you can"), ("PWYC", "Pay what you can"), ("£12 (£8 concs)", "£12"),
    ("£15.50 – £30.00 full price", "£15.50–£30"),            # Wilton's
    ("From: £0.00", "Free"), ("Tickets £10", "£10"), ("Free entry", "Free"),
    ("Free, booking required", "Free"), ("Donation", "Donation"), ("By donation", "Donation"),
    ("£5.00", "£5"), ("€12", "€12"), ("€8.50", "€8.50"), ("$20", "$20"), ("500 ₽", "500 ₽"),
    ("1 500 ₽", "1500 ₽"), ("от 1500 руб.", "From 1500 ₽"), ("бесплатно", "Free"), ("Вход свободный", "Free"),
    ("£12 + booking fee", "£12"), ("£12 plus booking fee", "£12"),
    # mined
    ("£13.50 – £28.00 full price | £11.00 – £25", "£13.50–£28"),   # Wilton's Music Hall
    ("£ 10.00", "£10"),                                          # Green Note
    ("From £39 (£35 + £4 transaction fee)", "From £39"),         # Barbican
    ("£20.50 (£19 + £1.50 transaction fee)", "£20.50"),          # Barbican
    ("From £43 (£39 + £4 transaction fee) + free essentials", "From £43"),  # Barbican
    ("Tickets from £25", "From £25"),                            # His Majesty's
    ("£20 on the door", "£20"),                                  # Dublin Castle
    ("Tickets: £25", "£25"),                                     # Soane's Museum
    ("£17.39", "£17.39"),                                        # Windmill Brixton
    ("£0-£10", "£0–£10"),                                      # Westminster Abbey
    ("£11 - £20 per ticket", "£11–£20"),                         # Chickenshed
    ("£21+ per ticket", "From £21"),                             # Chickenshed
    ("From £0.00", "Free"),                                      # London Transport Museum
    ("£5 Adults, £10 Children", "£5"),                           # Discover Story Centre
    ("Children (age 7-16): £3.00", "£3"),                        # Fan Museum
    ("Price: £11", "£11"),                                       # Etcetera Theatre
    ("£80.00", "£80"),                                           # Pizza Express Jazz Club
    ("Concession £5 per workshop, £70 for 14 week", "£5"),       # Barbican
    ("£11.99", "£11.99"),                                        # Young V&A
    ("£10 - £10", "£10"),
    ("£0 - £0", "Free"),
    ("Free – £5 suggested donation", "Free"),
]


@pytest.mark.parametrize("text,want", CASES)
def test_parse_price(text, want):
    assert parse_price_text(text) == want


@pytest.mark.parametrize("text", ["", None, "Book now", "Sold out", "Tickets", "15 Nov 7.30pm", 42])
def test_no_price(text):
    assert parse_price_text(text) is None


@pytest.mark.parametrize("block,want", [
    ("Jazz Night | Sun 15th Nov 7:30pm\nVenue: Green Note\n£ 18.00 -> /events/jazz", "£18"),
    ("Free Your Mind: a talk | Thu 19 Nov | Free entry -> /x", "Free"),
    ("Free Your Mind: a talk | Thu 19 Nov -> /x", None),          # title word only
    ("Show | 12 Nov – 17 Dec\nWilton's Music Hall\n£15.50 – £30.00 full price", "£15.50–£30"),
    ("Exhibition | On now until 31 Dec\nAdmission free", "Free"),
    ("Workshop\nPay what you can\n10am", "Pay what you can"),
    ("Title only\nno price here", None),
    ("", None),
    ("Gig | Fri 20 Nov\nFree", "Free"),
    ("Talk | free wifi in the foyer | £8", "£8"),
])
def test_find_price(block, want):
    assert find_price(block) == want


@pytest.mark.parametrize("text,want", [
    ("500 – 2 000 ₽", "500–2000 ₽"),
    ("1400-2300 ₽", "1400–2300 ₽"),
    ("от 1500 до 2500 руб.", "1500–2500 ₽"),
    ("£10–15", "£10–£15"),
    ("£10-12.50", "£10–£12.50"),
    ("£10 - 15 years", "£10"),                         # age, not an upper price
    ("FREE FOR MEMBERS, £10 non-members", "£10"),
    ("Free with museum admission, £5 otherwise", "£5"),
    ("£0 - £10", "£0–£10"),
])
def test_review_regressions(text, want):
    assert parse_price_text(text) == want


@pytest.mark.parametrize("text", ["от 0 рублей до 2 500 рублей", "£ 0 £25 £50", "Free for members",
                                  "Step free access", "Feel free to bring a friend"])
def test_not_free(text):
    assert parse_price_text(text) != "Free"


def test_free_after_access_note():
    assert parse_price_text("Step free access. Free entry") == "Free"
