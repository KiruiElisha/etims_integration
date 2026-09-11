"""
Integrity of the shipped demo catalogue.

The loader in ``seed.py`` reads this file at runtime on a real site, where a bad
row surfaces as a link-validation failure several hundred inserts in. These
tests read the CSV directly -- no Frappe -- so the file is checked against the
contract the loader depends on before it ever reaches a site.
"""

import csv
import os
import unittest

CATALOGUE = os.path.join(
	os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "demo_catalogue.csv"
)

# Duplicated from install.py rather than imported: that module imports frappe,
# and the point of these tests is that they run without a site.
BANDS = {"A-Exempt", "B-16.00%", "C-0%", "D-Non-VAT", "E-8%"}
PRODUCT_TYPES = {"01Raw Material", "02Finished Product", "03Service without stock"}

REQUIRED = (
	"PLUNo",
	"PLUName",
	"UnitPrice",
	"item_ClsCode",
	"pkgUnitCd",
	"qtyUnitCd",
	"OrgnNatCd",
	"TaxType",
	"TypeCode",
)


def rows():
	with open(CATALOGUE, newline="", encoding="utf-8-sig") as handle:
		return [r for r in csv.DictReader(handle) if (r.get("PLUNo") or "").strip()]


class TestDemoCatalogue(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.rows = rows()

	def test_ships_with_the_app(self):
		self.assertTrue(os.path.isfile(CATALOGUE), f"catalogue missing: {CATALOGUE}")

	def test_has_every_column_the_loader_reads(self):
		with open(CATALOGUE, newline="", encoding="utf-8-sig") as handle:
			header = csv.DictReader(handle).fieldnames
		for column in REQUIRED:
			self.assertIn(column, header)

	def test_row_count(self):
		self.assertEqual(len(self.rows), 479)

	def test_plu_numbers_are_unique(self):
		numbers = [r["PLUNo"].strip() for r in self.rows]
		self.assertEqual(len(numbers), len(set(numbers)))

	def test_plu_numbers_fit_the_item_code_padding(self):
		"""The loader builds ETIMS-DEMO-PLU-<PLUNo zero-padded to 4>."""
		for row in self.rows:
			self.assertTrue(row["PLUNo"].strip().isdigit())
			self.assertLess(int(row["PLUNo"]), 10000)

	def test_every_tax_band_is_a_real_band(self):
		"""
		A band the Select does not offer is stored anyway and then rejected by the
		device, which is a long way round to a typo.
		"""
		for row in self.rows:
			self.assertIn(row["TaxType"].strip(), BANDS)

	def test_every_product_type_is_real(self):
		for row in self.rows:
			self.assertIn(row["TypeCode"].strip(), PRODUCT_TYPES)

	def test_coded_fields_split_to_a_code(self):
		"""``BG-Bag`` -> ``BG``, and the code half is never empty."""
		for row in self.rows:
			for column in ("pkgUnitCd", "qtyUnitCd", "OrgnNatCd"):
				code = row[column].split("-", 1)[0].strip()
				self.assertTrue(code, f"{column} has no code half: {row[column]!r}")

	def test_coded_fields_split_to_the_expected_masters(self):
		"""
		Pins what the split actually yields, so a mangled conversion of the source
		workbook is caught here rather than as a link error mid-load. ``qtyUnitCd``
		is the one to watch: "U-Pieces/item [Number]" has punctuation after the
		code, and a sloppier split returns the label.
		"""
		codes = {
			column: {row[column].split("-", 1)[0].strip() for row in self.rows}
			for column in ("pkgUnitCd", "qtyUnitCd", "OrgnNatCd")
		}
		self.assertEqual(codes["pkgUnitCd"], {"BG"})
		self.assertEqual(codes["qtyUnitCd"], {"U"})
		self.assertEqual(codes["OrgnNatCd"], {"KE"})

	def test_names_fit_the_item_name_field(self):
		for row in self.rows:
			self.assertTrue(row["PLUName"].strip())
			self.assertLessEqual(len(row["PLUName"]), 140)

	def test_prices_are_numeric(self):
		for row in self.rows:
			float(row["UnitPrice"])

	def test_classification_codes_are_numeric(self):
		for row in self.rows:
			self.assertTrue(row["item_ClsCode"].strip().isdigit())

	def test_is_a_volume_fixture_not_a_tax_fixture(self):
		"""
		Guards the docstring's claim, and the division of labour with
		create_demo_items: this file is uniformly standard-rated, so anyone
		reaching for it to test the VAT bands is reaching for the wrong fixture.
		"""
		self.assertEqual({r["TaxType"].strip() for r in self.rows}, {"B-16.00%"})


if __name__ == "__main__":
	unittest.main()
