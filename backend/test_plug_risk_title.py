"""Pure-function tests for the mains-plug title filter used by the EU price-drop scan."""
import unittest

from app.services import sourcing_classifier as sc
from app.services.sourcing_classifier import title_suggests_mains_plug


class MainsPlugTitleTests(unittest.TestCase):
    def test_mains_powered_computer_gear_is_flagged(self):
        for title in (
            "Anker 65W USB-C GaN Charger, 3-Port Fast Wall Charger",
            "AVM FRITZ!Box 7490 WLAN AC + N Router (VDSL/ADSL, 1.300 MBit/s)",
            "Dell 24 Inch Full HD Monitor",
            "Brother HL-L2350DW Wireless Mono Laser Printer",
            "Corsair RM850x 850 Watt 80 Plus Gold PSU",
            "Corsair RM850x Power Supply Unit",
            "APC Back-UPS 700VA UPS Battery Backup",
            "Belkin 4-Way Surge Protector Extension Lead",
            "Original Netzteil 65W fuer Notebook",
            "TP-Link AX1800 Mesh WiFi System",
            "Synology DS220j 2-Bay NAS",
            "DIGITUS Network Switch - 5-Port Fast Ethernet - 4x RJ45 + 1x RJ45 Uplink",
            "TP-Link 8-Port Gigabit Desktop Switch",
            "Netgear 5-Port PoE Switch",
            "HP 15 Laptop, Intel Core i5",
            "Brother QL-820NWB Professional Label Printer",
            "Wireless Charger Stand 15W Fast Charging",
        ):
            self.assertTrue(title_suggests_mains_plug(title), title)

    def test_appliances_and_power_tools_are_flagged(self):
        for title in (
            "Tefal Express Steam Iron", "Russell Hobbs Electric Kettle 1.7L", "Ninja Air Fryer 4L",
            "Bosch 18V Cordless Drill Driver Kit incl. Charger", "Karcher K2 Pressure Washer", "Dyson V8 Cordless Vacuum Cleaner",
        ):
            self.assertTrue(title_suggests_mains_plug(title), title)

    def test_usb_powered_and_passive_accessories_are_kept(self):
        for title in (
            "Logitech G305 LIGHTSPEED Wireless Gaming Mouse",
            "Razer BlackWidow V3 Mechanical Gaming Keyboard",
            "Anker USB-C to HDMI Adapter 4K",
            "SanDisk 2TB Extreme Portable SSD",
            "Logitech C920 HD Pro Webcam",
            "Corsair HS80 RGB Wireless Gaming Headset",
            "Anker 20000mAh Portable Power Bank",
            "Ergotron LX Monitor Arm Desk Mount",
            "Wera Kraftform Screwdriver Set 6 pieces",
            "Elgato Stream Deck MK.2",
            "Corsair Vengeance 32GB DDR5 RAM",
        ):
            self.assertFalse(title_suggests_mains_plug(title), title)

    def test_car_chargers_have_no_mains_plug(self):
        self.assertFalse(title_suggests_mains_plug("Anker 48W Car Charger Dual USB-C"))
        self.assertFalse(title_suggests_mains_plug("In-Car USB Charger 12V"))

    def test_products_bought_via_eu_a2a_that_are_plug_free_are_kept(self):
        # Each of these is a real EU A2A purchase the filter wrongly flagged in the 2026-09-20 back-test.
        for title in (
            "Bialetti Moka Express Aluminium Stovetop Coffee Maker 130 ml (3 Cup)",
            "Bialetti New Venus Italian Coffee Maker (Induction), Stainless Steel",
            "INSTAX mini film format Link 3 smartphone photo printer, Clay White",
            "Dymo LetraTag LT-100H Label Maker Starter Kit | Handheld Label Printer Machine",
            "DYMO Authentic D1 Labels | Black Print on White | 12mm x 7m",
            "Belkin BoostCharge 3-Port Laptop Power Bank 20K with USB-C & USB-A Ports",
            "Ryobi OBL1820S 18V ONE+ Cordless Blower (Battery & Charger Excluded)",
            "hansgrohe Ecostat Comfort thermostatic shower mixer, chrome",
            "Logitech M500 Wired USB Mouse, works with laptop and desktop",
            "Brother PT-E110 Label Maker, P-Touch Electrician Label Printer, Handheld, QWERTY",
            "GoPro Dual Battery Charger + 2 Enduro Rechargeable Batteries (HERO13 Black)",
            "CORSAIR 3000D RGB AIRFLOW Mid-Tower PC Case - 3x AR120 RGB Fans",
            "Razer Leviathan V2 X - PC Gaming Soundbar (Full-Range Drivers, Compact Desktop Form Factor)",
            "Karcher Garment Steamer Attachment for ironable Clothing",
            "CanPick 963xl Multipack Printer Cartridges Compatible with Officejet Pro",
            "Fischer Thermax - Fixing System for Internal Walls, 20 pieces, Cordless Drill Compatible",
            "Bosch 2608644042 EXWOH 40 Tooth Precision Circular Saw Blade",
            "hansgrohe Talis M54 - kitchen mixer tap, 1 spray, pull-out spout 270 mm",
            "Makita ML007G 40V Max Li-ion XGT Cordless Work Light - Batteries and Chargers Not Included",
            "Nintendo Switch Pro Controller",
            "DIGITUS Pull-out shelf - 19-inch - 1U - Front and rear mounting",
        ):
            self.assertFalse(title_suggests_mains_plug(title), title)

    def test_power_bank_sold_with_a_wall_charger_is_still_flagged(self):
        self.assertTrue(title_suggests_mains_plug("20000mAh Power Bank with 30W Wall Charger Included"))

    def test_bialetti_electric_range_is_still_flagged(self):
        self.assertTrue(title_suggests_mains_plug("Bialetti Electric Kettle 1.7L"))

    def test_patterns_contain_no_control_characters(self):
        # A "\b" mangled into a literal backspace once silently turned every word boundary into
        # a character that matches nothing, disabling the whole filter.
        for pattern in (sc._MAINS_PLUG_TITLE_RE.pattern, sc._PLUG_FREE_TITLE_RE.pattern):
            self.assertFalse([c for c in pattern if ord(c) < 32], pattern[:60])

    def test_empty_titles_are_not_flagged(self):
        self.assertFalse(title_suggests_mains_plug(None))
        self.assertFalse(title_suggests_mains_plug(""))


if __name__ == "__main__":
    unittest.main()
