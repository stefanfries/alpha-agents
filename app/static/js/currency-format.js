(function (global) {
  var locale = (global.currencyLocale || 'de_DE').replace('_', '-');
  var numberFormat = new Intl.NumberFormat(locale, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  var localeParts = new Intl.NumberFormat(locale).formatToParts(12345.6);
  var groupSeparator = localeParts.find(function (part) { return part.type === 'group'; }).value;
  var decimalSeparator = localeParts.find(function (part) { return part.type === 'decimal'; }).value;

  function formatAmount(value) {
    return numberFormat.format(value);
  }

  function parseAmount(value) {
    var normalized = value.trim().split(groupSeparator).join('').replace(decimalSeparator, '.');
    if (!/^\d+(?:\.\d{1,2})?$/.test(normalized)) return null;
    var parsed = Number(normalized);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function bindInput(input, hiddenInput, minimum, step) {
    function sync() {
      var amount = parseAmount(input.value);
      var valid = amount !== null && amount >= minimum && Number.isInteger(amount)
        && (amount - minimum) % step === 0;
      var minimumLabel = formatAmount(minimum);
      var stepMessage = step > 1 ? ' in steps of ' + formatAmount(step) : '';
      input.setCustomValidity(valid ? '' : 'Enter a whole amount of at least ' + minimumLabel + stepMessage + '.');
      hiddenInput.value = valid ? String(amount) : '';
      return valid;
    }

    input.value = formatAmount(Number(hiddenInput.value));
    input.addEventListener('input', sync);
    input.addEventListener('blur', function () {
      if (sync()) input.value = formatAmount(parseAmount(input.value));
    });
    sync();
    return sync;
  }

  global.currencyFormat = { bindInput: bindInput, formatAmount: formatAmount, parseAmount: parseAmount };
})(window);