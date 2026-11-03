"""Regenerate Pinboard native startup and action documents from the reviewed owner map."""

from instruction_catalog import generate, validate_startup_composition

generate()
print(validate_startup_composition())
