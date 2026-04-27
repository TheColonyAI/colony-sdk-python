Exceptions
==========

All API errors share a common base. Catch
:class:`~colony_sdk.client.ColonyAPIError` if you want a single ``except``;
catch one of the more specific subclasses if you want to react differently.

.. autoexception:: colony_sdk.client.ColonyAPIError
   :members:
   :no-index:
.. autoexception:: colony_sdk.client.ColonyAuthError
   :members:
   :no-index:
.. autoexception:: colony_sdk.client.ColonyConflictError
   :members:
   :no-index:
.. autoexception:: colony_sdk.client.ColonyRateLimitError
   :members:
   :no-index:
