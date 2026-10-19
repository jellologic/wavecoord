# Albums API

- `POST /albums` with `{name, photo_ids}` returns the new album or 409 for a used name.
- `PUT /albums/order` with `{album_ids}` saves the order.
